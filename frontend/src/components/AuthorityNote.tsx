import type { Permission } from "../lib/api";

interface AuthorityNoteProps {
  allowed: boolean;
  permission: Permission;
  message?: string;
}

export function AuthorityNote({ allowed, permission, message }: AuthorityNoteProps) {
  if (allowed) return null;

  return (
    <p className="authority-note" role="note">
      <strong>Read-only at this boundary</strong>
      <span>{message ?? "Your signed role does not include this operation."}</span>
      <code>{permission}</code>
    </p>
  );
}
