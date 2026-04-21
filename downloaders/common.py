class Enum(object):
    def __init__(self, tuplelist):
        self.tuplelist = tuplelist

    def __getattr__(self, name):
        return self.tuplelist.index(name)


DownloadResult = Enum(('skipped', 'success', 'fallback_success', 'failure'))
