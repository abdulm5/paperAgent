class WorkflowStepError(RuntimeError):
    """Sanitized workflow failure safe for durable receipts and operators."""

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        code: str = "workflow_step_failed",
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class RetryableWorkflowError(WorkflowStepError):
    retryable = True


class PermanentWorkflowError(WorkflowStepError):
    retryable = False
