"""Train worker process.

Offline reranker training (ADR-0007). Thin entrypoint — the pipeline stages
live in ``packages/retrieval/training``.
"""

from ragos_train_worker.main import run

__all__ = ["run"]