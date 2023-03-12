import argparse
import logging
import os
import sys
import yaml

from pathlib import Path
from logging import DEBUG, INFO, WARNING, ERROR, CRITICAL
from email.utils import formatdate
from email.message import EmailMessage
from platform import node
from time import time
from nio import RoomLeaveResponse, JoinResponse, RoomForgetResponse

import mxmda
import mxmda.matrix

from mxmda.utils import existing_dir, XDGPaths

def arg_parser(name=None):
    """
    Define the application arguments. Use the parser() function to
    get the argument parser before any arguments have been consumed.
    This can be useful to analyze the available arguments, e.g. when
    generating documentation or shell completions.

    Returns an ArgumentParser with the supported arguments specified.
    """
    xdg = XDGPaths(name)
    argparser = argparse.ArgumentParser(prog=name)
    argparser.add_argument(
        '-V', '--version',
        action='version',
        version='%%(prog)s %s' % mxmda.__version__
    )
    argparser.add_argument(
        '-f', '--config',
        default=xdg.config("config.yml"),
        dest='config_file',
        type=Path,
        help='path to mxmda config file (default: %(default)s)',
    )
    argparser.add_argument(
        '-d', '--device-file',
        default=xdg.config("device.yml"),
        type=Path,
        help='path to mxmda device state file (created by mxmda) (default: %(default)s)',
    )
    argparser.add_argument(
        '-N', '--nio-dir',
        default=xdg.config("nio"),
        type=Path,
        help="nio's state dir (created by mxmda) (default: %(default)s)",
    )
    argparser.add_argument(
        '-q', '--quiet',
        action='count',
        # higher valued log level means higher importance; so --quiet
        # lowers verbosity level, but *increases* the actual log level.
        dest='log_level_incr',
        default=0,
        help='lower verbosity level (can be used multiple time)',
    )
    argparser.add_argument(
        '-v', '--verbose',
        action='count',
        # lower valued log level means lower importance; so --verbose
        # increases verbosity level, but *lowers* the actual log level.
        dest='log_level_decr',
        default=0,
        help='increase verbosity level (can be used multiple time)',
    )
    argparser.add_argument(
        'log_level',
        action='store_const',
        const=2,
        help=argparse.SUPPRESS,
    )

    subparsers = argparser.add_subparsers(dest='command', help='Commands')

    service = subparsers.add_parser(
        "service",
        help="Start a message poller, writing each message to a maildir"
    )
    service.add_argument(
        '--maildir', '-m',
        default=xdg.state('mail'),
        help="Maildir path (default: %(default)s)",
    )

    join = subparsers.add_parser(
        "join",
        help="Instruct the bot to join a specified room",
    )
    join.add_argument('rooms', nargs='*', help='Join these rooms')

    leave = subparsers.add_parser(
        "leave",
        help="Instruct the bot to leave a specified room",
    )
    leave.add_argument('rooms', nargs='*', help='Leave these rooms')

    rooms = subparsers.add_parser(
        "rooms",
        help="List joined rooms",
    )
    rooms.add_argument(
        '--room', '-r',
        dest='rooms',
        action='append',
        help="Only list rooms listed in arguments; flag can be repeated"
    )
    rooms.add_argument(
        '--list-users', '-u',
        action='store_true',
        help="List users joined to each room",
    )

    msg = subparsers.add_parser(
        "msg",
        help="Send a message to a specified target",
    )
    msg.add_argument(
        '--target', '-t',
        help="Send message to this target (room or user)",
    )
    msg.add_argument('msg', nargs=1, help='Message to send')

    return argparser

def log_level(n):
    levels = [DEBUG, INFO, WARNING, ERROR, CRITICAL]
    return levels[min(len(levels), max(0, n))]

def parse_args(name=None):
    p = arg_parser(name).parse_args(sys.argv[1:])

    # Map log_level to real `logging` level integer value
    p.log_level = log_level(p.log_level + p.log_level_incr - p.log_level_decr)

    return p

class Application:
    def __init__(self, args):
        self.logger = logging.getLogger(mxmda.__name__)
        self.logger.setLevel(args.log_level)

        with open(args.config_file) as fh:
            self.config = yaml.safe_load(fh)

        self.device_file = args.device_file
        self.load_device()

        self.client = mxmda.matrix.Client(
            app=self,
            nio_dir=args.nio_dir,
            config=self.config,
            device=self.device,
            log_level=args.log_level,
        )

    def write_device(self, device):
        self.logger.info("Updating device file, device id %s",
                         device.get('device_id'))
        state = {
            'access_token': device['access_token'],
            'device_id': device['device_id'],
            'user_id': device['user_id'],
        }
        with open(self.device_file, 'w') as fh:
            yaml.dump(state, fh)
        self.state = state

    def load_device(self, default=None):
        try:
            with open(self.device_file) as fh:
                self.device = yaml.safe_load(fh)
        except FileNotFoundError:
            self.device = {}

class Service(Application):
    def __init__(self, args):
        super().__init__(args)

        self.maildir = args.maildir
        for subdir in ('cur', 'new', 'tmp'):
            existing_dir(os.path.join(self.maildir, subdir))

        self.client.add_event_callback(write_event_to_maildir(self),
                                       mxmda.matrix.RoomMessageText)

    async def start(self):
        self.logger.debug("Starting client")
        await self.client.start()
        #await self.client.msg(self.matrix.room,
        #                      "i'm online now, awaiting interactions")
        self.logger.info("Matrix initialization complete, entering sync loop")
        await self.client.enter_loop()

def mxid_to_email(mxid):
    if not mxid.startswith(('@', '!', '#')):
        raise ValueError("Invalid mxid %s" % mxid)
    return mxid[1:].replace(':', '@', 1)

def event_to_email(room, event):
    text = event.body.strip()
    topline = text.split("\n", 1)[0]

    subject = topline if len(topline) < 70 else topline[0:67] + '...'

    mail = EmailMessage()
    mail.add_header("Subject", topline)
    mail.add_header("From", mxid_to_email(event.sender))
    mail.add_header("To", mxid_to_email(room.machine_name))
    mail.add_header("Message-Id", event.event_id)
    mail.add_header("Date", formatdate(event.server_timestamp/1000))
    mail.add_alternative(text)
    mail.add_alternative(bytes(yaml.dump(event.source, indent=2), 'utf-8'),
                         maintype='application', subtype='mxmda',
                         params={
                            'type': event.source['type'],
                            'charset': 'utf-8'
                         }, cte='8bit')

    return mail

def write_event_to_maildir(app):
    async def deliverer(room, event):
        filename = os.path.join(app.maildir, 'new', '_'.join([str(time()), node()]))
        mail = event_to_email(room, event)
        with open(filename, 'w') as fh:
            print(mail, file=fh)
    return deliverer

class Command(Application):
    async def start(self):
        self.logger.debug("Starting client")
        await self.client.start()
        self.logger.info("Matrix initialization complete")

class MsgCommand(Command):
    def __init__(self, args):
        super().__init__(args)
        self.target = args.room
        self.msg = args.msg

    async def start(self):
        await super().start()
        self.logger.info("Sending msg to room %s: %s", self.target, self.msg)
        await self.client.msg(self.target, self.msg)
        await self.client.sync()
        await self.client.close()

class RoomsCommand(Command):
    def __init__(self, args):
        super().__init__(args)
        self.rooms = args.rooms

class RoomlistCommand(Command):
    def __init__(self, args):
        super().__init__(args)
        self.list_users = args.list_users
        self.rooms = args.rooms

    def filter(self, room):
        return not self.rooms or room.room_id in self.rooms \
                              or room.machine_name in self.rooms

    def fmt(self, room):
        return "%s - %s <%s> (%s users)" % (
            room.machine_name, room.name, room.room_id, room.member_count
        )

    async def start(self):
        await super().start()
        for room in filter(self.filter, self.client.rooms.values()):
            print(self.fmt(room))
            if self.list_users:
                for n in room.users:
                    print(f' - {n}')
        await self.client.close()

class JoinCommand(RoomsCommand):
    async def start(self):
        self.logger.info("Joining rooms")
        await super().start()
        self.logger.info("Joining rooms: %s", self.rooms)
        for room in self.rooms:
            self.logger.info("Joining %s", room)
            resp = await self.client.join(room)
            if not isinstance(resp, JoinResponse):
                self.logger.error("Failed to join room %s: %s", room, resp)
        await self.client.sync()
        await self.client.close()

class LeaveCommand(RoomsCommand):
    async def start(self):
        self.logger.info("Leaving rooms")
        await super().start()
        self.logger.info("Leaving rooms: %s", self.rooms)
        for room in self.rooms:
            room_obj = await self.client.room_resolve_alias(room)
            room_id = room_obj.room_id
            self.logger.info("Leaving %s (%s) - %s", room, room_id, dir(room_id))
            resp = await self.client.room_leave(room_id)
            if not isinstance(resp, RoomLeaveResponse):
                self.logger.error("Failed to leave room %s (%s): %s", room, room_id, resp)
            resp = await self.client.room_forget(room_id)
            if not isinstance(resp, RoomForgetResponse):
                self.logger.error("Failed to foret room %s (%s): %s", room, room_id, resp)

        await self.client.sync()
        await self.client.close()

def command(args):
    return {
        'msg': MsgCommand,
        'join': JoinCommand,
        'leave': LeaveCommand,
        'rooms': RoomlistCommand,
        'service': Service,
    }[args.command](args)
