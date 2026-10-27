"""Task description: synthetic order SLA validation and controlled release."""

TASK_DESCRIPTION = """SLA Semantic Release Guard

Investigate a synthetic fulfillment feed whose columns and numeric types stay
the same while duration units change from seconds to milliseconds and rows change
from orders to packages. Validate the business contract, block unsafe publication,
correct the order-level SLA metric, verify the result, and demonstrate rollback.
"""

if __name__ == "__main__":
    print(TASK_DESCRIPTION)
