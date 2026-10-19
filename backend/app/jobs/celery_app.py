"""Celery 應用（broker＝Redis）。啟動 worker：

    celery -A app.jobs.celery_app worker -Q gpu --pool=solo -n gpu@%h        # 3D 重建
    celery -A app.jobs.celery_app worker -Q schedule --pool=solo -n sched@%h  # 排程求解

訊息只帶 job_id：授權、參數、版本都從工作資料庫讀，佇列裡沒有 JWT 或任何請求內容。
acks_late＋reject_on_worker_lost：worker 掛掉訊息會重新投遞，重複投遞由 store.claim 擋掉。
"""

from celery import Celery

from app.core.config import get_settings

RUN_TASK = "artrag.run_job"
QUEUES = ("gpu", "schedule")


def make_app() -> Celery:
    s = get_settings()
    broker = s.redis_url or "redis://127.0.0.1:1/0"  # 沒設定時指向不存在的位址，不退回記憶體 broker
    app = Celery("artrag", broker=broker, include=["app.jobs.tasks"])
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_backend=None,
        task_ignore_result=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_default_queue="schedule",
        broker_connection_retry_on_startup=True,
        # API／dispatcher 發佈時 broker 不通要快速失敗（留在 outbox 稍後重試），不能卡住請求
        broker_connection_timeout=2,
        broker_transport_options={"socket_connect_timeout": 2, "socket_timeout": 5},
        task_publish_retry=True,
        task_publish_retry_policy={
            "max_retries": 1,
            "interval_start": 0,
            "interval_step": 0.2,
            "interval_max": 0.5,
        },
        timezone="UTC",
        enable_utc=True,
    )
    return app


celery = make_app()
