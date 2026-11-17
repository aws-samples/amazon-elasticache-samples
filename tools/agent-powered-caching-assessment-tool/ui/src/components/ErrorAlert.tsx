import { Alert, Box, Button, SpaceBetween } from "@cloudscape-design/components";
import { ApiError, guidance } from "../api/errors";

interface Props {
  error: unknown;
  header?: string;
  onRetry?: () => void;
}

/** An API failure: the backend's code and detail, the request id, and what to do. */
export function ErrorAlert({ error, header, onRetry }: Props) {
  const e = error instanceof ApiError
    ? error
    : new ApiError(0, "client_error", error instanceof Error ? error.message : String(error), null);
  const help = guidance(e);
  return (
    <Alert
      type="error"
      header={header ?? "Request failed"}
      action={onRetry ? <Button onClick={onRetry}>Retry</Button> : undefined}
    >
      <SpaceBetween size="xxs">
        {help && <Box>{help}</Box>}
        <Box>
          <Box variant="code">{e.code}</Box> {e.detail}
        </Box>
        {e.requestId && (
          <Box variant="small" color="text-body-secondary">
            Request ID: <Box variant="code" fontSize="body-s">{e.requestId}</Box>
          </Box>
        )}
      </SpaceBetween>
    </Alert>
  );
}
