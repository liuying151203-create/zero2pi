"""模型请求的公共运行时组件。"""

from __future__ import annotations

from typing import Any

from zero2pi.ui import format_model_request


class ModelRequester:
    """封装模型客户端、模型名称和统一请求状态输出。

    作用：让每个章节的 main() 只负责组装依赖，不再在函数内部定义模型请求逻辑。
    输入：已创建的模型客户端、模型名称、请求超时时间，以及本次 API 请求参数。
    输出：模型客户端返回的响应对象；客户端异常统一转换为 RuntimeError。
    流程：打印请求状态 → 调用 Messages API → 将底层异常包装后抛出。
    """

    def __init__(self, client: Any, model: str, timeout_seconds: float) -> None:
        self.client = client
        self.model = model
        self.timeout_seconds = timeout_seconds

    def __call__(self, **kwargs: Any) -> Any:
        """发送一次模型请求。"""
        print(format_model_request(self.timeout_seconds), flush=True)
        try:
            return self.client.messages.create(model=self.model, **kwargs)
        except Exception as error:
            raise RuntimeError(f"模型请求失败：{error}") from error
