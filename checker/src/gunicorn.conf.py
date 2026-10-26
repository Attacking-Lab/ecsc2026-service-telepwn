import multiprocessing

worker_class = "uvicorn.workers.UvicornWorker"
workers = min(8, multiprocessing.cpu_count())
bind = "0.0.0.0:8000"
timeout = 90
keepalive = 3600
preload_app = True
