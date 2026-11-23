from __future__ import annotations

from fastapi import FastAPI

from app.api.routes_corrections import router as corrections_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="会员空间平台 · 提成人工调整与重算接口",
        version="1.0.0",
        description=(
            "提成规则表模块的更正操作：人工调整（双人审批、留痕）与回款/合同更正后的重算。"
            "金额单位一律为分；身份上下文由 S2-IAM 提供（本地用请求头桩）。"
        ),
    )
    app.include_router(corrections_router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "commission-corrections", "version": "1.0.0"}

    return app


app = create_app()
