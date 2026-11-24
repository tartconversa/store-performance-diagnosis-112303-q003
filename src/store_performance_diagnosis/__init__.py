"""门店经营异常诊断与行动效果回看。"""

from .config import APP_VERSION

__all__ = ["APP_VERSION"]
__version__ = APP_VERSION


def __getattr__(name: str):
    # 延迟导入，避免仅读取版本时加载数据库层。
    if name == "StoreDiagnosisApp":
        from .app import StoreDiagnosisApp

        return StoreDiagnosisApp
    raise AttributeError(name)
