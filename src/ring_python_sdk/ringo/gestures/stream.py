"""Lazy exports of the original density stream; use its push_frame method."""


def __getattr__(name):
    if name in ('SwipeDensityStream', 'StreamInference'):
        from ._upstream.swipe_density import stream
        return getattr(stream, name)
    raise AttributeError(name)
