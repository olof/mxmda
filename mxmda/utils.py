import os
import mxmda

def existing_dir(name):
    os.makedirs(name, exist_ok=True)
    return name

class XDGPaths:
    def __init__(self, name=None):
        self.name = name or mxmda.__name__

    def _file(self, cls, filename):
        p = str(XDGPath(cls, self.name))
        if filename is not None:
            p = os.path.join(p, filename)
        return p

    def config(self, filename=None):
        return self._file('config', filename)

    def state(self, filename=None):
        return self._file('state', filename)

class XDGPath:
    envs = {
        'config': ("XDG_CONFIG_HOME", os.path.join("{home}", ".config")),
        'state': ("XDG_STATE_HOME", os.path.join("{home}", ".local", "state")),
    }

    def __init__(self, cls, name='mxmda'):
        self.cls = cls
        self.name = name

    def __str__(self):
        return os.path.join(self.env.format(home=os.environ["HOME"]), self.name)

    @property
    def env(self):
        return os.environ.get(*self.envs[self.cls])
