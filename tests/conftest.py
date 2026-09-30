"""pytest 全局配置。

坑记录：部分测试用 `mocker.patch.dict(sys.modules, {...})` 注入假的第三方模块，
teardown 时会把「该测试期间新导入的模块」从 sys.modules 移除。
若 cv2 正好是在被 patch 的块内首次导入，就会被卸载，
后续测试重新导入时 cv2.dnn 初始化不完整：
    AttributeError: module 'cv2.dnn' has no attribute 'DictValue'
这里在收集阶段预先导入 cv2，保证它不会被意外卸载，使测试与执行顺序无关。
"""
try:
    import cv2  # noqa: F401
except Exception:  # pragma: no cover - 环境缺 cv2 时不影响其他用例
    pass
