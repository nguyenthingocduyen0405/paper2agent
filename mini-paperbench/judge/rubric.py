"""루브릭 JSON에서 리프 노드와 그 앞 맥락(preceding criteria)을 뽑는다."""
import json
from pathlib import Path


def load_leaves(rubric_path: Path) -> list[dict]:
    """리프마다 {id, requirements, task_category, preceding} 를 루브릭 순서대로 돌려준다.

    preceding: 원본 TaskNode.get_prior_nodes와 같은 규칙
    (조상 + 앞선 형제 + 조상의 앞선 형제)의 requirements 목록.
    """
    root = json.loads(Path(rubric_path).read_text())
    leaves: list[dict] = []

    def walk(node: dict, prior: list[str]) -> None:
        if not node["sub_tasks"]:
            leaves.append(
                {
                    "id": node["id"],
                    "requirements": node["requirements"],
                    "task_category": node.get("task_category"),
                    "preceding": prior,
                }
            )
            return
        prior = prior + [node["requirements"]]
        for i, child in enumerate(node["sub_tasks"]):
            siblings = [s["requirements"] for s in node["sub_tasks"][:i]]
            walk(child, prior + siblings)

    walk(root, [])
    return leaves
