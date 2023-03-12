import asyncio
import logging
import sys

import mxmda
from mxmda.app import command, parse_args
from mxmda.errors import UserError

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  [%(levelname)-8s] %(name)s: %(message)s',
        datefmt='%m-%d %H:%M'
    )

    try:
        asyncio.run(command(parse_args(name=mxmda.__name__)).start())
    except UserError as exc:
        logging.getLogger(mxmda.__name__).error(exc)
        sys.exit(1)
