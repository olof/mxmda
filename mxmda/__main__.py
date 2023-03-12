import asyncio
import logging
import sys

import mxmda
from mxmda.app import command, parse_args
from mxmda.errors import UserError

# https://github.com/pypa/setuptools/issues/1995
# Proper solution specified in comment by @Xophmeister:
# > In the current system, you have to delimit module and the
# > entrypoint callable with a colon. What if the logic was
# > something like:
# >
# > Split on:
# >  If there are two components, then proceed as currently;
# >  If there is only one component, then interpret this as
# >  a direct module invocation?
def issue_1995():
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

if __name__ == '__main__':
    issue_1995()
