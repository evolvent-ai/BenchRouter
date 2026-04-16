"""
Simulates an existing benchmark codebase that was NOT written for BenchRouter.

The only change needed to integrate with BenchRouter:
  - Read model URL from $BENCHROUTER_MODEL_ENDPOINT (was hardcoded before)
  - Write results to $BENCHROUTER_OUTPUT_DIR (was hardcoded before)

Note: The output uses "pass@1" as the key, not "overall".
      This is handled by result_mapping in task.yaml.
"""

import os
import json
from openai import OpenAI

# The only line changed from the original code:
# was: MODEL_URL = "http://localhost:8000/v1"
MODEL_URL = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "http://localhost:8000/v1")
OUTPUT_DIR = os.environ.get("BENCHROUTER_OUTPUT_DIR", "./output")


def run_evaluation():
    client = OpenAI(base_url=MODEL_URL, api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"))
    model = os.environ.get("BENCHROUTER_MODEL_NAME", "default")

    # Simulate code generation tasks
    tasks = [
        {"task_id": "task_1", "prompt": "Write a Python function that returns the sum of two numbers.", "test": "assert add(1, 2) == 3"},
        {"task_id": "task_2", "prompt": "Write a Python function that checks if a number is even.", "test": "assert is_even(4) == True"},
    ]

    passed = 0
    for task in tasks:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": task["prompt"]}],
            temperature=0,
        )
        code = resp.choices[0].message.content
        # In real HumanEval, you'd execute the code and run tests
        # Here we just check if it looks like a function definition
        if "def " in code:
            passed += 1

    # Output uses the original field names, NOT "overall"
    results = {
        "pass@1": passed / len(tasks),
        "pass@10": passed / len(tasks),  # simplified
        "total_tasks": len(tasks),
        "passed_tasks": passed,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "scores.json"), "w") as f:
        json.dump(results, f, indent=2)

    print(f"pass@1: {results['pass@1']:.2f}")


if __name__ == "__main__":
    run_evaluation()
