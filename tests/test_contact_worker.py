import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKER_TEST = ROOT / "contact-worker" / "test" / "index.test.mjs"


def test_contact_worker_sends_only_the_customer_confirmation() -> None:
    node = shutil.which("node")

    assert node is not None, "Node.js is required to run the Worker test"
    result = subprocess.run(
        [node, "--test", str(WORKER_TEST)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
