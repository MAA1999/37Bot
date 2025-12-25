from .plugin import MirrorChyanPlugin

# 修正模块名以便 loader 能找到插件类
MirrorChyanPlugin.__module__ = __name__

__all__ = ["MirrorChyanPlugin"]
