# -*- coding: utf-8 -*-
from gevent import monkey
monkey.patch_all()

import os
from datetime import datetime
from gevent import Greenlet
from gevent import spawn, sleep
from gevent.queue import Empty
from iso8601 import parse_date
from pytz import timezone
from requests.exceptions import ConnectionError
import logging
import logging.config
import time
from openprocurement_client.exceptions import (
    InvalidResponse,
    RequestFailed,
    ResourceNotFound,
    ResourceGone
)

logger = logging.getLogger(__name__)

TZ = timezone(os.environ['TZ'] if 'TZ' in os.environ else 'Europe/Kiev')


class ResourceItemWorker(Greenlet):

    def __init__(self, api_clients_queue=None, resource_items_queue=None,
                 db=None, config_dict=None, retry_resource_items_queue=None,
                 api_clients_info=None):
        Greenlet.__init__(self)
        self.exit = False
        self.update_doc = False
        self.db = db
        self.config = config_dict
        self.api_clients_queue = api_clients_queue
        self.resource_items_queue = resource_items_queue
        self.retry_resource_items_queue = retry_resource_items_queue
        self.bulk = {}
        self.bulk_save_limit = self.config['bulk_save_limit']
        self.bulk_save_interval = self.config['bulk_save_interval']
        self.start_time = datetime.now()
        self.api_clients_info = api_clients_info

    def add_to_retry_queue(self, resource_item, status_code=0):
        timeout = resource_item.get('timeout') or\
            self.config['retry_default_timeout']
        retries_count = resource_item.get('retries_count') or 0
        if status_code != 429:
            resource_item['timeout'] = timeout * 2
            resource_item['retries_count'] = retries_count + 1
        else:
            resource_item['timeout'] = timeout
            resource_item['retries_count'] = retries_count
        if resource_item['retries_count'] > self.config['retries_count']:
            logger.critical(
                '{} {} reached limit retries count {} and dropped from '
                'retry_queue.'.format(
                    self.config['resource'][:-1].title(),
                    resource_item['id'], self.config['retries_count']),
                extra={'MESSAGE_ID': 'dropped_documents'})
        else:
            self.retry_resource_items_queue.put(resource_item)
            sleep(timeout)
            logger.info('Put {} {} to \'retries_queue\''.format(
                self.config['resource'][:-1], resource_item['id']),
                extra={'MESSAGE_ID': 'add_to_retry'})

    def _get_api_client_dict(self):
        if not self.api_clients_queue.empty():
            try:
                api_client_dict = self.api_clients_queue.get(
                    timeout=self.config['queue_timeout'])
                logger.debug('GET API CLIENT: {}'.format(api_client_dict['id']),
                             extra={'MESSAGE_ID': 'get_client'})
                logger.debug('SLEEP before return client: {}'.format(api_client_dict['request_interval']))
            except Empty:
                return None
            if self.api_clients_info[api_client_dict['id']]['drop_cookies']:
                try:
                    api_client_dict['client'].renew_cookies()
                    self.api_clients_info[api_client_dict['id']] = {
                        'drop_cookies': False,
                        'request_durations': {},
                        'request_interval': 0,
                        'avg_duration': 0
                    }
                    api_client_dict['request_interval'] = 0
                    api_client_dict['not_actual_count'] = 0
                    logger.info('Drop lazy api_client {} cookies'.format(
                        api_client_dict['id']))
                except (Exception, ConnectionError) as e:
                    self.api_clients_queue.put(api_client_dict)
                    logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                                 extra={'MESSAGE_ID': 'put_client'})
                    logger.error('While renewing cookies catch exception: '
                                 '{}'.format(e.message))
                    return None

            logger.debug('Got api_client ID: {} {}'.format(
                api_client_dict['id'],
                api_client_dict['client'].session.headers['User-Agent']))
            return api_client_dict
        else:
            return None

    def _get_resource_item_from_queue(self):
        if not self.resource_items_queue.empty():
            queue_resource_item = self.resource_items_queue.get(
                timeout=self.config['queue_timeout'])
            logger.debug('Get {} {} from main queue.'.format(
                self.config['resource'][:-1], queue_resource_item['id']))
            return queue_resource_item
        else:
            return None

    def _get_resource_item_from_public(self, api_client_dict,
                                       queue_resource_item):
        retry_key = 'rev' if self.config['historical'] else 'dateModified'
        try:
            logger.debug('Request interval {} sec. for client {}'.format(
                api_client_dict['request_interval'],
                api_client_dict['client'].session.headers['User-Agent']))
            start = time.time()
            if self.config['historical']:
                resource_item = api_client_dict['client'].get_resource_item_historical(
                    queue_resource_item['id'], queue_resource_item['rev']
                ).get('data')
                resource_item['rev'] = str(queue_resource_item['rev'])
            else:
                resource_item = api_client_dict['client'].get_resource_item(
                    queue_resource_item['id']
                ).get('data')
            self.api_clients_info[api_client_dict['id']][
                'request_durations'][datetime.now()] = time.time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] =\
                api_client_dict['request_interval']
            log_value = resource_item['rev'] if self.config['historical'] else resource_item['dateModified']
            logger.debug('Received from API {}: {}-{}'.format(
                self.config['resource'][:-1], resource_item['id'],
                log_value))
            if api_client_dict['request_interval'] > 0:
                api_client_dict['request_interval'] -=\
                    self.config['client_dec_step_timeout']
            if not self.config['historical'] and resource_item['dateModified'] <\
                    queue_resource_item['dateModified']:
                logger.info(
                    'Client {} got not actual {} document {} from public '
                    'server.'.format(
                        api_client_dict['client'].session.headers[
                            'User-Agent'], self.config['resource'][:-1],
                        queue_resource_item['id']),
                    extra={'MESSAGE_ID': 'not_actual_docs'})
                self.add_to_retry_queue({
                    'id': queue_resource_item['id'],
                    'dateModified': queue_resource_item['dateModified']
                })
                self.api_clients_queue.put(api_client_dict)
                logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                             extra={'MESSAGE_ID': 'put_client'})
                return None  # Not actual
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                         extra={'MESSAGE_ID': 'put_client'})
            return resource_item
        except ResourceGone:
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                         extra={'MESSAGE_ID': 'put_client'})
            logger.info(
                '{} {} archived.'.format(self.config['resource'][:-1].title(),
                                         queue_resource_item['id'])
            )
            return None  # Archived
        except InvalidResponse as e:
            self.api_clients_info[api_client_dict['id']][
                'request_durations'][datetime.now()] = time.time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] =\
                api_client_dict['request_interval']
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                         extra={'MESSAGE_ID': 'put_client'})
            logger.error(
                'Error while getting {} {} from public with status code: '
                '{}'.format(
                    self.config['resource'][:-1], queue_resource_item['id'],
                    e.status_code), extra={'MESSAGE_ID': 'exceptions'})
            self.add_to_retry_queue({
                'id': queue_resource_item['id'],
                retry_key: queue_resource_item[retry_key]
            })
            return None
        except RequestFailed as e:
            self.api_clients_info[api_client_dict['id']][
                'request_durations'][datetime.now()] = time.time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] =\
                api_client_dict['request_interval']
            if e.status_code == 429:
                if (api_client_dict['request_interval'] >
                        self.config['drop_threshold_client_cookies']):
                    api_client_dict['client'].session.cookies.clear()
                    api_client_dict['request_interval'] = 0
                else:
                    api_client_dict['request_interval'] +=\
                        self.config['client_inc_step_timeout']
                self.api_clients_queue.put(api_client_dict)
                logger.warning(
                    'PUT API CLIENT: {} after {} sec.'.format(
                        api_client_dict['id'],
                        api_client_dict['request_interval']),
                    extra={'MESSAGE_ID': 'put_client'})
            else:
                self.api_clients_queue.put(api_client_dict)
                logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                             extra={'MESSAGE_ID': 'put_client'})
            logger.error(
                'Request failed while getting {} {} from public with status '
                'code {}: '.format(
                    self.config['resource'][:-1], queue_resource_item['id'],
                    e.status_code), extra={'MESSAGE_ID': 'exceptions'})
            self.add_to_retry_queue({
                'id': queue_resource_item['id'],
                retry_key: queue_resource_item[retry_key]
            }, status_code=e.status_code)
            return None  # request failed
        except ResourceNotFound as e:
            self.api_clients_info[api_client_dict['id']][
                'request_durations'][datetime.now()] = time.time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] =\
                api_client_dict['request_interval']
            log_value = queue_resource_item['rev'] if self.config['historical'] else queue_resource_item['dateModified']
            logger.error('Resource not found {} at public: {}-{}. {}'.format(
                self.config['resource'][:-1], queue_resource_item['id'],
                log_value, e.message),
                extra={'MESSAGE_ID': 'not_found_docs'})

            api_client_dict['client'].session.cookies.clear()
            logger.info('Clear client cookies')
            self.add_to_retry_queue({
                'id': queue_resource_item['id'],
                retry_key: queue_resource_item[retry_key]
            })
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                         extra={'MESSAGE_ID': 'put_client'})
            return None  # not found
        except Exception as e:
            self.api_clients_info[api_client_dict['id']][
                'request_durations'][datetime.now()] = time.time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] =\
                api_client_dict['request_interval']
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                         extra={'MESSAGE_ID': 'put_client'})
            log_value = queue_resource_item['rev'] if self.config['historical'] else queue_resource_item['dateModified']
            logger.error(
                'Error while getting resource item {} {}-{} from public '
                '{}: '.format(
                    self.config['resource'][:-1], queue_resource_item['id'],
                    log_value, e.message),
                extra={'MESSAGE_ID': 'exceptions'})

            self.add_to_retry_queue({
                'id': queue_resource_item['id'],
                retry_key: queue_resource_item[retry_key]
            })
            return None

    def _add_to_bulk(self, resource_item, resource_item_doc=None):
        resource_item['doc_type'] = self.config['resource'][:-1].title()
        if self.config['historical']:
            resource_item['_id'] = resource_item['id'] + '-' + resource_item['rev']
        else:
            resource_item['_id'] = resource_item['id']
        if resource_item_doc:
            resource_item['_rev'] = resource_item_doc['_rev']
        bulk_doc = self.bulk.get(resource_item['id'])

        if self.config['historical']:
            self.bulk[resource_item['id']] = resource_item
            logger.debug('Put in bulk {} {}-{}'.format(
                self.config['resource'][:-1], resource_item['id'],
                resource_item['rev']),
                extra={'MESSAGE_ID': 'add_to_save_bulk'})
        else:
            if bulk_doc and bulk_doc['dateModified'] <\
                    resource_item['dateModified']:
                logger.debug(
                    'Replaced {} in bulk {} previous {}, current {}'.format(
                        self.config['resource'][:-1], bulk_doc['id'],
                        bulk_doc['dateModified'], resource_item['dateModified']),
                    extra={'MESSAGE_ID': 'skipped'})
                self.bulk[resource_item['id']] = resource_item
            elif bulk_doc and bulk_doc['dateModified'] >=\
                    resource_item['dateModified']:
                logger.debug(
                    'Ignored duplicate {} {} in bulk: previous {}, current '
                    '{}'.format(
                        self.config['resource'][:-1], resource_item['id'],
                        bulk_doc['dateModified'], resource_item['dateModified']),
                    extra={'MESSAGE_ID': 'skipped'})
            if not bulk_doc:
                self.bulk[resource_item['id']] = resource_item
                logger.debug('Put in bulk {} {} {}'.format(
                    self.config['resource'][:-1], resource_item['id'],
                    resource_item['dateModified']),
                    extra={'MESSAGE_ID': 'add_to_save_bulk'})
        return

    def _save_bulk_docs(self):
        if (len(self.bulk) > self.bulk_save_limit or
                (datetime.now() - self.start_time).total_seconds() >
                self.bulk_save_interval or self.exit):
            try:
                logger.debug('Try save bulk: {}'.format(len(self.bulk)),
                             extra={'SAVE_BULK_LEN': len(self.bulk)})
                start = time.time()
                res = self.db.update(self.bulk.values())
                end = time.time() - start
                logger.debug('Bulk save duration: {} sec.'.format(end),
                             extra={'SAVE_BULK_DURATION': end})
                if not self.config['historical']:
                    for resource_item in self.bulk.values():
                        ts = (datetime.now(TZ) -
                              parse_date(resource_item[
                                  'dateModified'])).total_seconds()
                        logger.debug('{} {} timeshift is {} sec.'.format(
                            self.config['resource'][:-1], resource_item['id'], ts),
                            extra={'DOCUMENT_TIMESHIFT': ts})
                logger.info('Save bulk {} docs to db.'.format(len(self.bulk)))
            except Exception as e:
                logger.error('Error while saving bulk_docs in db: {}'.format(
                    e.message), extra={'MESSAGE_ID': 'exceptions'})
                for doc in self.bulk.values():
                    if self.config['historical']:
                        self.add_to_retry_queue({
                            'id': doc['id'],
                            'rev': doc['rev']
                        })
                    else:
                        self.add_to_retry_queue({
                            'id': doc['id'],
                            'dateModified': doc['dateModified']
                        })
                self.bulk = {}
                self.start_time = datetime.now()
                return
            self.bulk = {}
            for success, doc_id, rev_or_exc in res:
                if success:
                    if not rev_or_exc.startswith('1-'):
                        logger.info('Update {} {}'.format(
                            self.config['resource'][:-1], doc_id),
                            extra={'MESSAGE_ID': 'update_documents'})
                    else:
                        logger.info('Save {} {}'.format(
                            self.config['resource'][:-1], doc_id),
                            extra={'MESSAGE_ID': 'save_documents'})
                    continue
                elif not self.config['historical']:
                    if rev_or_exc.message !=\
                            u'New doc with oldest dateModified.':
                        self.add_to_retry_queue({'id': doc_id,
                                                 'dateModified': None})
                        logger.error(
                            'Put to retry queue {} {} with reason: '
                            '{}'.format(self.config['resource'][:-1],
                                        doc_id, rev_or_exc.message))
                    else:
                        logger.debug('Ignored {} {} with reason: {}'.format(
                            self.config['resource'][:-1], doc_id, rev_or_exc),
                            extra={'MESSAGE_ID': 'skipped'})
                        continue
            self.start_time = datetime.now()

    def _run(self):
        while not self.exit:
            # Try get api client from clients queue
            api_client_dict = self._get_api_client_dict()
            if api_client_dict is None:
                logger.debug('API clients queue is empty.')
                sleep(self.config['worker_sleep'])
                continue
            # Try get item from resource items queue
            queue_resource_item = self._get_resource_item_from_queue()
            if queue_resource_item is None:
                self.api_clients_queue.put(api_client_dict)
                logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                             extra={'MESSAGE_ID': 'put_client'})
                logger.debug('Resource items queue is empty.')
                sleep(self.config['worker_sleep'])
                continue

            resource_item_doc = None
            # Try get resource item from local storage
            if not self.config['historical']:
                try:
                    # Resource object from local db server
                    resource_item_doc = self.db.get(queue_resource_item['id'])
                    if queue_resource_item['dateModified'] is None:
                        public_doc = self._get_resource_item_from_public(
                            api_client_dict, queue_resource_item)
                        if public_doc:
                            queue_resource_item['dateModified'] \
                                = public_doc['dateModified']
                        else:
                            continue
                        api_client_dict = self._get_api_client_dict()
                        if api_client_dict is None:
                            self.add_to_retry_queue(queue_resource_item)
                            continue
                    if (resource_item_doc and
                            resource_item_doc['dateModified'] >=
                            queue_resource_item['dateModified']):
                        logger.debug('Ignored {} {} QUEUE - {}, EDGE - {}'.format(
                            self.config['resource'][:-1],
                            queue_resource_item['id'],
                            queue_resource_item['dateModified'],
                            resource_item_doc['dateModified']),
                            extra={'MESSAGE_ID': 'skiped'})
                        self.api_clients_queue.put(api_client_dict)
                        logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                                     extra={'MESSAGE_ID': 'put_client'})
                        continue
                except Exception as e:
                    self.api_clients_queue.put(api_client_dict)
                    logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']),
                                 extra={'MESSAGE_ID': 'put_client'})
                    self.add_to_retry_queue({
                        'id': queue_resource_item['id'],
                        'dateModified': queue_resource_item['dateModified']
                    })
                    logger.error('Error while getting resource item from couchdb: '
                                 '{}'.format(repr(e)),
                                 extra={'MESSAGE_ID': 'exceptions'})
                    continue

            # Try get resource item from public server
            resource_item = self._get_resource_item_from_public(
                api_client_dict, queue_resource_item)
            if resource_item is None:
                continue

            # Add docs to bulk
            self._add_to_bulk(resource_item, resource_item_doc)

            # Save/Update docs in db
            self._save_bulk_docs()

    def shutdown(self):
        self.exit = True
        logger.info('Worker complete his job.')
