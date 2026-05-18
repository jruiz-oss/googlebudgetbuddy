/**
 * Designed empty state — icon + headline + body + optional CTA.
 *
 * <EmptyState
 *   icon={Inbox}
 *   title="No campaigns tracked yet"
 *   body="Connect a Meta ad account and import campaigns to start pacing."
 *   action={{ label: 'Add Account', onClick: () => ... }}
 * />
 */
import React from 'react';

export function EmptyState({ icon, title, body, action, secondary }) {
  return (
    <div className="bb-empty">
      {icon && (
        <div className="bb-empty-icon" aria-hidden="true">
          {icon}
        </div>
      )}
      <div className="bb-empty-title">{title}</div>
      {body && <div className="bb-empty-body">{body}</div>}
      {(action || secondary) && (
        <div className="bb-empty-actions">
          {action && (
            <button
              type="button"
              className="bb-btn bb-btn-primary"
              onClick={action.onClick}
            >
              {action.icon && <action.icon size={14} aria-hidden="true" />}
              {action.label}
            </button>
          )}
          {secondary && (
            <button
              type="button"
              className="bb-btn bb-btn-secondary"
              onClick={secondary.onClick}
            >
              {secondary.icon && <secondary.icon size={14} aria-hidden="true" />}
              {secondary.label}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default EmptyState;
