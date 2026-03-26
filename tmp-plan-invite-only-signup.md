# Plan: Invite-Only Sign-Up (Option A)

## Goal
Block all self-service sign-ups that don't have a valid pending invite.
Only users with a valid invite token in the URL can create a Cognito account.
Internal users are always created by an admin — never affected.

## Chosen Approach: PreSignUp Lambda Trigger

Instead of disabling `selfSignUpEnabled` entirely (which would break the sign-up
form we need for the invite flow), we add a **PreSignUp Lambda trigger** that
checks whether the registering email has a valid PENDING invite before allowing
the Cognito account to be created.

PreSignUp fires synchronously — if it throws, Cognito blocks the sign-up and
shows the user an error. If it returns the event, sign-up proceeds.

This keeps the existing invite flow intact:
  /signup?invite=<token> → store token in sessionStorage → Cognito hosted UI
  sign-up → PreSignUp checks email has valid invite → sign-up allowed →
  PostConfirmation adds to external group → /signup/complete → complete-registration
  writes membership record.

## Edge Cases Handled

1. **No invite, tries to sign up** → PreSignUp blocks with clear error message.
2. **Invite exists but expired** → PreSignUp blocks (expires_at check).
3. **Invite already CONSUMED** → PreSignUp blocks.
4. **Invite email doesn't match sign-up email** → PreSignUp blocks.
5. **Internal user signs up** → PreSignUp fires too — internal users must NOT
   go through self-signup. They are admin-created. PreSignUp will block them
   since they won't have an invite. This is correct behaviour.
6. **Existing users (already confirmed)** → PreSignUp does NOT fire for existing
   users logging in — only on new sign-up attempts. Existing accounts are unaffected.
7. **Race condition: two people use same invite simultaneously** → Both pass
   PreSignUp (invite is still PENDING at that point). complete-registration has
   conditional writes that handle this — second one gets a 409. The first
   Cognito account that completes registration gets the membership. The other
   account is orphaned (no membership, lands on PendingAccessPage).
8. **Invite token not yet in sessionStorage when Cognito hosted UI loads** →
   The PreSignUp trigger checks the email against the invites table by email,
   not by token. The token is NOT passed to PreSignUp — Cognito doesn't forward
   custom params to this trigger. So the check is: does this email have any
   valid PENDING unexpired invite?
9. **Admin-created users (CfnUserPoolUser)** → Use adminConfirmSignUp flow,
   not self-registration flow. PreSignUp does NOT fire for AdminCreateUser.
   Unaffected.
10. **Password reset** → No sign-up trigger involved. Unaffected.

## What Changes

### New Lambda: pre-signup/index.py
- Reads email from event["request"]["userAttributes"]["email"]
- Queries FAST-stack-org-invites GSI: email-index (new GSI on invited_email)
- If no valid PENDING unexpired invite for this email → raise Exception (blocks sign-up)
- If valid invite found → return event (allows sign-up)
- Reads: INVITES_TABLE_NAME env var

### New DynamoDB GSI on InvitesTable
- indexName: "invited_email-index"
- partitionKey: invited_email (STRING)
- projectionType: ALL
- Needed so PreSignUp Lambda can query by email efficiently

### cognito-stack.ts changes
- Add PreSignUp Lambda construct (similar to PostConfirmation)
- Wire via CfnUserPool addPropertyOverride("LambdaConfig.PreSignUp", arn)
- Add Lambda invoke permission for cognito-idp.amazonaws.com
- Pass INVITES_TABLE_NAME env var

### backend-stack.ts changes
- Add invited_email-index GSI to InvitesTable
- Grant pre-signup Lambda read access to invites table
- Pass invites table name to pre-signup Lambda (via SSM or direct env — direct env
  not possible since invites table is in backend stack and pre-signup Lambda is
  in cognito stack — need SSM or pass table name as prop)

### Architecture note: cross-stack table name passing
  CognitoStack is a NestedStack. InvitesTable is created in BackendStack.
  BackendStack runs createOrgTenancyInfra() which builds InvitesTable.
  Pre-signup Lambda needs INVITES_TABLE_NAME at Lambda creation time.

  Options:
  A. Pass invites table name as a prop from FastMainStack to CognitoStack
     (FastMainStack creates BackendStack first, then passes table name to CognitoStack)
  B. Use SSM: BackendStack writes table name to SSM, pre-signup Lambda reads at runtime
     (runtime SSM read in Lambda handler — adds latency to every sign-up)
  C. Use Lambda env var set after both stacks are created (requires post-deploy step)

  **Chosen: Option A** — pass table name as a prop from FastMainStack.
  FastMainStack already instantiates BackendStack before CognitoStack.
  We expose invitesTableName as a public readonly on BackendStack and pass it
  to CognitoStack as a new prop.

## Files to Change

1. `infra-cdk/lambdas/pre-signup/index.py` — NEW
2. `infra-cdk/lib/backend-stack.ts` — expose `invitesTableName`, add invited_email-index GSI
3. `infra-cdk/lib/cognito-stack.ts` — add PreSignUp Lambda + trigger wiring + new prop
4. `infra-cdk/lib/fast-main-stack.ts` — pass invitesTableName from BackendStack to CognitoStack

## What Does NOT Change
- complete-registration Lambda — unchanged
- InviteAcceptPage.tsx — unchanged
- InviteCompletePage.tsx — unchanged
- post-confirmation Lambda — unchanged (still adds confirmed users to external group)
- presign-upload Lambda — unchanged (membership check stays)
- Frontend routing — unchanged

## Deployment Note
Adding a GSI to InvitesTable (new table, just deployed) is a single GSI add —
no CloudFormation double-GSI conflict since invitesTable currently has only one GSI
(org_id-index). Adding invited_email-index is one operation → fine.
