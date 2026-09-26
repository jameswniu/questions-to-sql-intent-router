import { useState } from "react";

import { switchUser } from "@/lib/api";
import { initials } from "@/lib/format";
import type { SessionView } from "@/lib/types";

/**
 * Who the questions run as. In demo mode anyone may pick a user, which starts a new session and reloads the page; it
 * stands in for signing in. Behind the sign-in proxy the page only names the user.
 */
export function IdentityPicker({ session }: { session: SessionView }) {
  const [switching, setSwitching] = useState(false);
  const { me } = session;
  if (!session.demo) {
    return (
      <div className="identity-grid">
        <span className="text-xs font-medium text-subtle">Asking as</span>
        <strong className="who truncate text-sm font-medium">
          {me.name}, {me.title}
        </strong>
      </div>
    );
  }
  return (
    <div className="identity-grid">
      <label htmlFor="user" className="hidden text-xs font-medium text-subtle md:block">
        Asking as
      </label>
      <select
        id="user"
        aria-label="Asking as"
        defaultValue={me.user_id}
        disabled={switching}
        onChange={(event) => {
          const chosen = event.currentTarget.value;
          setSwitching(true);
          void switchUser(chosen).then((ok) => {
            if (ok) location.reload();
            else setSwitching(false);
          });
        }}
        className="select-native h-9 w-full min-w-0 cursor-pointer truncate rounded-lg border border-control pl-3 text-sm font-medium text-foreground shadow-xs transition-colors hover:border-subtle disabled:cursor-progress md:w-auto md:max-w-[420px]"
      >
        {session.users.map((user) => (
          <option key={user.user_id} value={user.user_id}>
            {user.name}, {user.title}
          </option>
        ))}
      </select>
      <p className="demo-note text-xs leading-snug text-subtle">Demo identity, stands in for SSO</p>
    </div>
  );
}

/** The signed-in user, named with their initials, for a page that needs no picker. */
export function WhoChip({ session }: { session: SessionView }) {
  const { me } = session;
  return (
    <div className="flex min-w-0 items-center justify-end gap-2.5">
      <span
        aria-hidden="true"
        className="grid size-8 shrink-0 place-items-center rounded-full bg-accent-soft text-xs font-semibold text-accent-text"
      >
        {initials(me.name)}
      </span>
      <span className="min-w-0 leading-tight">
        <span className="block truncate text-sm font-medium">{me.name}</span>
        <span className="hidden truncate text-xs text-subtle sm:block">{me.title}</span>
      </span>
    </div>
  );
}
