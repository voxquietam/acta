# ADR 0010: Permissions and Onboarding

**Status:** accepted
**Date:** 2026-05-15

## Context

Acta is multi-workspace from day one (see [0003](0003-hierarchy.md)), even if KSU24 will run a single workspace initially. The permission model has to answer: who can do what inside a workspace, what's privacy granularity (workspace vs project), and how new users join.

The target team is ~10 people, all known to each other. Heavy enterprise-style permissions would be overhead without benefit; a simple three-tier role model covers real needs.

## Decisions

### Membership model

```
WorkspaceMember
  user         FK(User)
  workspace    FK(Workspace)
  role         CharField   # owner | admin | member
  joined_at    auto_now_add

  Meta: unique_together = (user, workspace)
```

A user is "in" a workspace iff a `WorkspaceMember` row exists. No implicit/public access.

### Roles

- **Owner** — workspace creator. Exactly one per workspace. Can transfer ownership and delete the workspace.
- **Admin** — manages members, projects, labels. Cannot delete the workspace or remove the owner.
- **Member** — full task-level work: create/edit/delete tasks and comments, post project updates, manage labels, create projects.

### Permissions matrix

| Action                                         | Owner | Admin | Member |
|------------------------------------------------|:-----:|:-----:|:------:|
| Read everything in the workspace               |   ✓   |   ✓   |   ✓    |
| Create / edit / delete any task                |   ✓   |   ✓   |   ✓    |
| Create comments and project updates            |   ✓   |   ✓   |   ✓    |
| Edit / delete **own** comment or update        |   ✓   |   ✓   |   ✓    |
| Delete **others'** comment or update           |   ✓   |   ✓   |   ✗    |
| Create / edit / archive project                |   ✓   |   ✓   |   ✓    |
| **Delete** project                             |   ✓   |   ✓   |   ✗    |
| Create / edit / delete labels and label groups |   ✓   |   ✓   |   ✓    |
| Invite / remove members                        |   ✓   |   ✓   |   ✗    |
| Change role (member ↔ admin)                   |   ✓   |   ✓   |   ✗    |
| Transfer ownership                             |   ✓   |   ✗   |   ✗    |
| Delete workspace                               |   ✓   |   ✗   |   ✗    |

Admin can promote another member to admin or demote an admin, but cannot touch the owner.

### Privacy granularity

No per-project privacy in MVP. All workspace members see all projects in the workspace. Adding project-level ACLs later requires a new join table and queryset filter — no destructive migration.

### Onboarding flow

**Open login + admin grants membership.**

1. Anyone with a Google account can log in via Google OAuth (`django-allauth`).
2. On first login, a `User` row is created (email + name from Google profile). No `WorkspaceMember` is created automatically.
3. The user lands on a "You're not in any workspace yet. Ask an admin to add you." screen.
4. An owner or admin opens the workspace members page, enters the user's email, picks a role. The system finds the matching `User` row by email and creates the `WorkspaceMember`. If no User exists yet, the admin can still pre-fill the email — see Open Questions.
5. On next page load, the user has access.

## Why

- **Three-tier roles** match the actual team shape: one tech lead (owner), maybe one or two co-admins, the rest are members. Two tiers would conflate "can manage workspace" with "can do work"; four-plus tiers would over-engineer.
- **Members can delete others' tasks** because Acta is a shared tracker, not a personal todo. Locking edits to "author only" creates noise for legitimate cleanup. Activity log records who deleted, so accountability is preserved. Revisit if it causes pain.
- **Members can create projects and manage labels** because in a 10-person team, gating these creates more friction than the protection is worth.
- **No per-project privacy** is the right default for one team. Adds back as a new entity (`ProjectMember`) only when a real "private project" need arrives.
- **Open login + manual membership** is the simplest secure-ish flow: no signup gating, no domain whitelist, no pre-issued invitation tokens. An anonymous Google-authed user with no membership is harmless — they see nothing.

## Consequences

- DRF needs permission classes: `IsWorkspaceMember`, `IsWorkspaceAdmin`, `IsWorkspaceOwner`, `IsAuthorOrAdmin` (for own-vs-others comment/update edits).
- Every queryset on Task/Project/Comment/Label/etc must filter by `workspace__members__user=request.user`. Helper mixin recommended to avoid forgetting one endpoint.
- Member-list UI needs role picker and "remove member" action. Owner is special: cannot be removed via the standard "remove" path; only via ownership transfer.
- Ownership transfer is a single endpoint: `POST /api/workspaces/{id}/transfer-ownership/ {new_owner_user_id}`. Swaps roles atomically.
- Workspace deletion cascades to all members, projects, tasks, comments, etc. Confirmed via UI prompt; no undo.

## Open Questions

- **Pre-Google invitations:** if admin types an email for a user who hasn't logged in yet, should we create a placeholder membership keyed by email and resolve it on first login? Or block until the user has logged in once? Lean toward "create placeholder, resolve later" — to be decided at implementation.
- **Self-service onboarding for trusted domains:** could auto-create membership for `@ksu.ks.ua` emails into a designated workspace. Deferred — adds complexity for marginal benefit while the team is small.
- **Audit of admin actions** (member added/removed, role changed): worth recording in ActivityLog at a workspace level. To be specified in the activity-log ADR.
