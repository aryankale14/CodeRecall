import time
from collections import OrderedDict, deque

# In-memory log store: maps repo_url -> bounded queue of {time, message} entries.
#
# Both bounds matter: a large repository emits one entry per file, and every
# repository ever scanned used to keep its entries for the lifetime of the
# process. Nothing evicted them, so this dict only ever grew.
MAX_ENTRIES_PER_REPO = 500
MAX_TRACKED_REPOS = 100

_pipeline_logs: "OrderedDict[str, deque]" = OrderedDict()


def add_pipeline_log(repo_url: str, message: str):
    """Append a timestamped log entry for a repository pipeline."""
    if repo_url not in _pipeline_logs:
        _pipeline_logs[repo_url] = deque(maxlen=MAX_ENTRIES_PER_REPO)
        # Evict the least recently used repository once we exceed the cap.
        while len(_pipeline_logs) > MAX_TRACKED_REPOS:
            _pipeline_logs.popitem(last=False)

    _pipeline_logs[repo_url].append({
        "time": time.strftime("%H:%M:%S"),
        "message": message
    })
    _pipeline_logs.move_to_end(repo_url)
    print(f"[LOG] {message}")


def get_pipeline_logs(repo_url: str) -> list[dict]:
    """Return all retained log entries for a repository."""
    return list(_pipeline_logs.get(repo_url, ()))


def clear_pipeline_logs(repo_url: str):
    """Clear all log entries for a repository."""
    _pipeline_logs.pop(repo_url, None)
