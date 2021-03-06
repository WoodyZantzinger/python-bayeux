'''
    Copyright (c) 2016, Salesforce.org
    All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of Salesforce.org nor the names of
      its contributors may be used to endorse or promote products derived
      from this software without specific prior written permission.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
    "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
    LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
    FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
    COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
    INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
    BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
    LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
    CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
    LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
    ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
    POSSIBILITY OF SUCH DAMAGE.
'''

import simplejson as json
import gevent
import gevent.queue
import requests
import requests.exceptions
from datetime import datetime
from copy import deepcopy

import logging
LOG = logging.getLogger('python_bayeux')

# See https://docs.cometd.org/current/reference/#_bayeux for bayeux reference


class BayeuxClient(object):
    def __init__(self, endpoint=None, oauth_session=None, start=True):
        self.endpoint = endpoint
        self.oauth_session = \
            requests.Session() if oauth_session is None else oauth_session
        self.shutdown_called = False
        self.shutdown_completed = False

        # Inbound
        self.message_queue = gevent.queue.Queue()

        # Outbound
        self.subscription_queue = gevent.queue.Queue()
        self.unsubscription_queue = gevent.queue.Queue()
        self.publication_queue = gevent.queue.Queue()

        self.channel_ids = {}

        self.subscription_callbacks = {}

        # handshake() has a side effect of initializing self.message_counter
        self.handshake()

        self.connect_timeout = None
        self.disconnect_complete = False
        self.executing = False
        self.stop_greenlets = False
        self.waiting_for_resubscribe = False
        self.go_called = False
        self.exception = None

        self.outbound_greenlets = []
        for method in (self._subscribe_greenlet,
                       self._unsubscribe_greenlet,
                       self._publish_greenlet):
            new_greenlet = gevent.Greenlet(method)
            new_greenlet.link_exception(self._exception_callback)
            self.outbound_greenlets.append(new_greenlet)

        self.inbound_greenlets = []
        connect_greenlet = gevent.Greenlet(self._connect_greenlet)
        connect_greenlet.link_exception(self._exception_callback)
        self.inbound_greenlets.append(connect_greenlet)

        self.greenlets = self.outbound_greenlets + self.inbound_greenlets

        if start:
            self.start()

    def handshake(self, **kwargs):
        self.message_counter = 1

        handshake_payload = {
            # MUST
            'channel': '/meta/handshake',
            'supportedConnectionTypes': ['long-polling'],
            'version': '1.0',
            # MAY
            'id': None,
            'minimumVersion': '1.0'
        }
        handshake_payload.update(kwargs)
        handshake_response = self._send_message(handshake_payload)

        # TODO: No error checking here
        self.client_id = handshake_response[0]['clientId']

        # Connect one time to get the server's timeout advive
        initial_connect_response = self.connect(initial=True)
        initial_connect_response_payload = initial_connect_response[0]

        # TODO: handle other 'reconnect' values
        if initial_connect_response_payload['successful']:
            # Convert to seconds
            if 'advice' in initial_connect_response_payload:
                advice = initial_connect_response_payload['advice']
                self.connect_timeout = advice['timeout'] / 1000.0
        # TODO: if not successful, then what?

    def disconnect(self):
        disconnect_response = self._send_message({
            # MUST
            'channel': '/meta/disconnect',
            'clientId': None,
            # MAY
            'id': None
        })
        self.disconnect_complete = True
        return disconnect_response

    def connect(self, initial=False):
        connect_request_payload = {
            # MUST
            'channel': '/meta/connect',
            'connectionType': 'long-polling',
            'clientId': None,
            # MAY
            'id': None
        }

        timeout = None if initial else self.connect_timeout

        connect_response = self._send_message(
            connect_request_payload,
            timeout=timeout
        )

        return connect_response

    def _send_message(self, payload, **kwargs):
        if 'id' in payload:
            payload['id'] = str(self.message_counter)
            self.message_counter += 1

        if 'clientId' in payload:
            payload['clientId'] = self.client_id

        LOG.info('_send_message(): payload: {0}  kwargs: {1}'.format(
            str(payload),
            str(kwargs)
        ))

        response = self.oauth_session.post(
            self.endpoint,
            data=json.dumps(payload),
            **kwargs
        )

        LOG.info(
            u'_send_message(): response status code: {0}  '
            u'response.text: {1}'.format(
                response.status_code,
                response.text
            )
        )

        if len(response.text) == 0:
            return ''

        return response.json()

    def _connect_greenlet(self):
        connect_response = None

        while not self.stop_greenlets:
            try:
                connect_response = self.connect()
            except requests.exceptions.ReadTimeout:
                LOG.info(
                    'connect greenlet timed out {0}'.format(
                        datetime.now()
                    )
                )
            else:
                if not isinstance(connect_response, list):
                    raise UnexpectedConnectResponseException(
                        str(connect_response)
                    )

                messages = []
                handshake_required = False
                for element in connect_response:
                    channel = element['channel']

                    if channel == '/meta/connect':
                        if not element['successful'] and \
                           element['error'] == '403::Unknown client':

                            # TODO: support handshake advice interval
                            if element['advice']['reconnect'] == 'handshake':
                                handshake_required = True
                    else:
                        # We got a push!
                        messages.append(element)

                if len(messages) > 0:
                    self.message_queue.put(messages)

                if handshake_required:
                    self.handshake()
                    self._resubscribe()

    def _execute_greenlet(self):
        self.executing = True
        channel = None
        while True:
            try:
                message_queue_messages = self.message_queue.get(
                    timeout=1
                )
            except gevent.queue.Empty:
                if self.stop_greenlets:
                    LOG.info(
                        'execute greenlet is stopping: '
                        'client id {0} at {1}'.format(
                            self.client_id,
                            str(datetime.now())
                        )
                    )
                    break
                else:
                    LOG.info(
                        'execute greenlet is NOT stopping: '
                        'client id {0} at {1}'.format(
                            self.client_id,
                            str(datetime.now())
                        )
                    )
                    continue

            LOG.info('client id {0} found message info {1} at {2}'.format(
                self.client_id,
                message_queue_messages,
                datetime.now()
            ))

            while self.waiting_for_resubscribe:
                gevent.sleep(0.5)

            for message_queue_message in message_queue_messages:
                channel = message_queue_message['channel']
                for callback in self.subscription_callbacks[channel]:
                    getattr(self, callback)(message_queue_message)

    def subscribe(self, channel, callback=None, **kwargs):
        LOG.info('enqueueing subscription for channel {0}'.format(
            channel
        ))
        subscription_queue_message = {
            'channel': channel
        }
        subscription_queue_message.update(kwargs)

        if channel not in self.subscription_callbacks:
            self.subscription_callbacks[channel] = []
            self.subscription_queue.put(subscription_queue_message)

        self.subscription_callbacks[channel].append(callback)

    def _resubscribe(self):
        self.waiting_for_resubscribe = True

        current_subscriptions = deepcopy(self.subscription_callbacks)
        self.subscription_callbacks.clear()
        for channel, callbacks in current_subscriptions.items():
            for callback in callbacks:
                self.subscribe(channel, callback)

        self.waiting_for_resubscribe = False

    def _subscribe_greenlet(self, successive_timeout_threshold=20,
                            timeout_wait=5):
        channel = None

        successive_timeouts = 0
        while True:
            try:
                subscription_queue_message = self.subscription_queue.get(
                    timeout=1
                )
                channel = subscription_queue_message['channel']
            except gevent.queue.Empty:
                if self.stop_greenlets:
                    break
                else:
                    continue

            subscribe_request_payload = {
                # MUST
                'channel': '/meta/subscribe',
                'subscription': channel,
                'clientId': None,
                # MAY
                'id': None
            }

            subscribe_responses = []
            try:
                subscribe_responses = self._send_message(
                    subscribe_request_payload
                )
            except requests.exceptions.ReadTimeout:
                successive_timeouts += 1

                if successive_timeouts > successive_timeout_threshold:
                    raise RepeatedTimeoutException('subscribe')

                gevent.sleep(timeout_wait)
                self.subscription_queue.put(subscription_queue_message)
            else:
                successive_timeouts = 0

            for element in subscribe_responses:
                if not element['successful'] and \
                   element['error'] == '403::Unknown client':
                    # Just try again, and eventually connect() will re-try a
                    # handshake
                    self.subscription_queue.put(subscription_queue_message)

    def unsubscribe(self, subscription):
        LOG.info('enqueueing unsubscription for channel {0}'.format(
            subscription
        ))
        self.unsubscription_queue.put(subscription)

    def _unsubscribe_greenlet(self, successive_timeout_threshold=20,
                              timeout_wait=5):
        successive_timeouts = 0
        while True:
            unsubscription = None
            try:
                unsubscription = self.unsubscription_queue.get(timeout=1)
            except gevent.queue.Empty:
                if self.stop_greenlets:
                    break
                else:
                    continue

            unsubscribe_request_payload = {
                # MUST
                'channel': '/meta/unsubscribe',
                'subscription': unsubscription,
                'clientId': None,
                # MAY
                'id': None
            }

            try:
                self._send_message(unsubscribe_request_payload)
            except requests.exceptions.ReadTimeout:
                successive_timeouts += 1

                if successive_timeouts > successive_timeout_threshold:
                    raise RepeatedTimeoutException('unsubscribe')

                gevent.sleep(timeout_wait)
                self.unsubscription_queue.put(unsubscription)
            else:
                successive_timeouts = 0

    def publish(self, channel, payload):
        self.publication_queue.put({
            'channel': channel,
            'payload': payload
        })

    def _publish_greenlet(self):
        while True:
            publication = None
            try:
                publication = self.publication_queue.get(timeout=1)
            except gevent.queue.Empty:
                if self.stop_greenlets:
                    break
                else:
                    continue

            channel = publication['channel']

            # Note that this return value isn't going anywhere.
            publish_request_payload = {
                # MUST
                'channel': channel,
                'data': publication['payload'],
                # MAY
                'clientId': None,
                'id': None
            }

            # Directly raise exceptions
            publish_response = self._send_message(publish_request_payload)

            LOG.info('publish response: {0}'.format(str(publish_response)))

    def start(self):
        for greenlet in self.outbound_greenlets:
            greenlet.start()
        for greenlet in self.inbound_greenlets:
            greenlet.start()

    # This is how a client can be given its own execute greenlet, but not
    # block the main greenlet
    def go(self):
        if self.go_called:
            return

        self.go_called = True
        block_greenlet = gevent.Greenlet(self.block)
        block_greenlet.link_exception(self._exception_callback)
        self.greenlets.append(block_greenlet)
        block_greenlet.start()
        # give the execute greenlet a chance to start, so self.executing is
        # True if we call block() later
        gevent.sleep(0.1)

    def block(self):
        if not self.executing:
            self._execute_greenlet()
        else:
            # block the main greenlet
            while self.exception is None and any(self.greenlets):
                gevent.sleep(1)

        if self.exception is not None:
            raise self.exception

    def shutdown(self):
        if not self.shutdown_called:
            self.shutdown_called = True

            self.stop_greenlets = True

            LOG.info('client id {0} is shutting down'.format(self.client_id))

            # disconnect() runs in the main greenlet, so we want to give the
            # others a chance to finish
            # If we have been called by a callback (that is, the client wants
            # to shut down itself), then we don't want to wait for the execute
            # greenlet to stop, because we'll deadlock.
            # This also means that subclasses should not change self.greenlets
            relevant_greenlets = \
                self.greenlets[:-1] \
                if gevent.getcurrent() == self.greenlets[-1] \
                else self.greenlets

            gevent.joinall(relevant_greenlets)
            self.disconnect()
            self.shutdown_completed = True

    def _exception_callback(self, failed_greenlet):
        LOG.info(
            'client id {0} has an unhandled exception '
            'in greenlet {1}: {2}'.format(
                self.client_id,
                failed_greenlet.name,
                str(failed_greenlet.exception)
            )
        )

        self.exception = failed_greenlet.exception

        try:
            self.shutdown()
        except Exception:
            # We prefer to shut down, but if something doesn't work (like the
            # org is sending back a total requests limit error), then don't
            # worry
            pass

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.shutdown()
        while not self.shutdown_completed:
            gevent.sleep(0.5)


class RepeatedTimeoutException(Exception):
    def __init__(self, greenlet_name):
        self.greenlet_name = greenlet_name
        super(RepeatedTimeoutException, self).__init__(
            'Too many timeouts'
        )


class UnexpectedConnectResponseException(Exception):
    def __init__(self, message):
        super(UnexpectedConnectResponseException, self).__init__(message)
