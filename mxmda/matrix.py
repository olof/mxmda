import asyncio
import html
import logging
import requests
from urllib.parse import urlparse

from nio import (
    AsyncClient, ClientConfig, MatrixRoom,
    ToDeviceError, LocalProtocolError,
    Event, RoomMessageText, AccountDataEvent, EphemeralEvent, ToDeviceEvent,
    LoginResponse, SyncResponse, UpdateReceiptMarkerResponse,

    KeyVerificationEvent,
    KeyVerificationStart,
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
)
from nio.store.database import DefaultStore

from mxmda.utils import existing_dir
from mxmda.errors import MatrixAuthError

def autodiscover_hs(uid):
    url = f"https://{uid[1:].split(':')[1]}/.well-known/matrix/client"
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json()['m.homeserver']['base_url']

class Client(AsyncClient):
    def __init__(self, *args,
                 app,
                 config,
                 nio_dir,
                 device=None,
                 nio_store=DefaultStore,
                 log_level=logging.INFO,
                 timeout=30,
                 **kwargs):
        device = device or {}
        hs = config.get('homeserver') or autodiscover_hs(config['user'])
        super().__init__(
            hs, config['user'],
            device_id=device.get('device_id'),
            store_path=existing_dir(nio_dir),
            config=ClientConfig(store=nio_store, store_sync_tokens=True),
            **kwargs
        )

        self.mxmda = app
        self.timeout = timeout * 1000
        self.mxmda_device = device
        self.mxmda_config = config

        self.add_log_callbacks(info=log_level <= logging.INFO,
                               debug=log_level <= logging.DEBUG)
        self.add_response_callback(sync(self.mxmda), SyncResponse)
        self.add_to_device_callback(key_verify(self.mxmda),
                                    KeyVerificationEvent)

        if self.mxmda_device:
            self.access_token = self.mxmda_device['access_token']
            self.device_id = self.mxmda_device['device_id']
            self.user_id = self.mxmda_device['user_id']
            self.load_store()

    def add_log_callbacks(self, info=True, debug=False):
        if info: self.add_info_log_callbacks()
        if debug: self.add_debug_log_callbacks()

    def add_info_log_callbacks(self):
        cb = debug(self.mxmda, logging.INFO)
        self.add_event_callback(cb, Event)
        self.add_to_device_callback(cb, ToDeviceEvent)

    def add_debug_log_callbacks(self):
        cb = debug(self.mxmda)
        self.add_response_callback(cb, Event)
        self.add_ephemeral_callback(cb, EphemeralEvent)
        self.add_global_account_data_callback(cb, AccountDataEvent)
        self.add_room_account_data_callback(cb, AccountDataEvent)

    def msg_pre(self, dest, text):
        return self.msg(dest, text,
                        html="<pre><code>%s</code></pre>" % html.escape(text))

    def msg(self, dest, text, html=None):
        if html is None:
            return self.room_send(dest, 'm.room.message', {
                'msgtype': 'm.text',
                'body': text,
            })
        return self.room_send(dest, 'm.room.message', {
            'msgtype': 'm.text',
            'body': text,
            'format': 'org.matrix.custom.html',
            'formatted_body': html
        })

    async def login(self):
        resp = await super().login_raw(self.mxmda_config['auth'])
        if not isinstance(resp, LoginResponse):
            raise MatrixAuthError("Matrix authentication failed: %s" % resp)
        self.mxmda.logger.info("Logged in: %s", resp)
        self.mxmda.write_device({
            "access_token": resp.access_token,
            "device_id": resp.device_id,
            "user_id": resp.user_id,
        })

    async def start(self):
        if not self.access_token:
            self.mxmda.logger.info("No access_token available, logging in")
            await self.login()

        self.mxmda.logger.info("Doing initial matrix state sync")
        await self.sync(timeout=10 * 1000, full_state=True)
        self.mxmda.logger.info("Matrix state sync complete")

    async def enter_loop(self):
        await self.sync_forever(timeout=self.timeout, full_state=True)

        # Something made us cleanly leave the sync_forever loop. What could
        # cause that?  While I don't think closing the client connection is
        # our biggest concern in this case, I see no point in removing this.
        # Note well that I have not actually seen this.
        await self.close()

async def read_receipt(app, room, event):
    marker = await app.client.update_receipt_marker(room.room_id,
                                                    event.event_id,
                                                    'm.read')
    if not isinstance(marker, UpdateReceiptMarkerResponse):
        app.logger.warning("Unable to update read marker for %s in %s",
                           event.event_id, room.room_id)

def msg_callback(app, name, callback):
    """
    Takes a callback and wrap it in common behavior for message handling,
    including event logging and sending read receipts.
    """
    async def common(room, event):
        app.logger.info("Considering msg event for %s callback", name)
        await asyncio.gather(read_receipt(app, room, event),
                             callback(room, event))
    return common

def sync(app):
    async def syncer(response):
        app.logger.debug("Processing sync response")
        if app.client.should_upload_keys:
            app.logger.debug("Uploading keys")
            res = await app.client.keys_upload()
            app.logger.debug("keys uploaded, result: %s", res)
        if app.client.should_query_keys:
            app.logger.debug("Querying missing keys")
            res = await app.client.keys_query()
            app.logger.debug("keys queried, result: %s", res)
        if app.client.should_claim_keys:
            users_for_key_claiming = client.get_users_for_key_claiming()
            app.logger.debug("Claiming keys: %s", users_for_key_claiming)
            res = await app.client.keys_claim(users_for_key_claiming)
            app.logger.debug("keys claimed, result: %s", res)
    return syncer

def debug(app, level=logging.DEBUG):
    def render(a):
        if isinstance(a, MatrixRoom):
            return a.room_id
        return a
    async def logger(*args):
        app.logger.log(level, "Event:\n%s", '\n'.join(
          ["  %s: %s" % (type(a).__name__, render(a)) for a in args]
        ))
    return logger

# The following has security implications. That's why i hidden it away here.
def key_verify(app):
    def sas(event):
        return app.client.key_verifications[event.transaction_id]

    async def start(event):
        if "emoji" not in event.short_authentication_string:
            app.logger.warning("Got auth request without support for icons; %s",
                               event.short_authentication_string)
            return
        resp = await app.client.accept_key_verification(event.transaction_id)
        if isinstance(resp, ToDeviceError):
            app.logger.error("accept_key_verification failed with %s", resp)

        resp = await app.client.to_device(sas(event).share_key())
        if isinstance(resp, ToDeviceError):
            app.logger.error("to_device failed with %s", resp)

    async def cancel(event):
        app.logger.info("Auth request cancelled")

    async def key(event):
        app.logger.warning("Got auth emojis: %s; will assume they match, "
                           "because secure system are for cowards", sas(event).get_emoji())
        resp = await app.client.confirm_short_auth_string(event.transaction_id)
        if isinstance(resp, ToDeviceError):
            app.logger.error("Unable to send the key confirmation for %s: %s",
                             event.transaction_id, resp)
            return

    async def mac(event):
        try:
            resp = await app.client.to_device(sas(event).get_mac())
            if isinstance(resp, ToDeviceError):
                app.logger.error("Failed to send final mac confirmation for %s: %s",
                                 event.transaction_id, resp)
                return
            else:
                app.logger.info("Enrolled new device; verified devices now: %s",
                                sas(event).verified_devices)
        except LocalProtocolError as exc:
            # e.g. it might have been cancelled by ourselves
            app.logger.error("Key exchange %s cancelled: %s",
                             event.transaction_id, exc)

    async def verify(event):
        return await {
            KeyVerificationStart: start,
            KeyVerificationCancel: cancel,
            KeyVerificationKey: key,
            KeyVerificationMac: mac,
        }[type(event)](event)

    return verify
