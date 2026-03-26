import * as cdk from "aws-cdk-lib"
import * as cognito from "aws-cdk-lib/aws-cognito"
import * as lambda from "aws-cdk-lib/aws-lambda"
import * as iam from "aws-cdk-lib/aws-iam"
import * as logs from "aws-cdk-lib/aws-logs"
import * as cr from "aws-cdk-lib/custom-resources"
import { Construct } from "constructs"
import { AppConfig } from "./utils/config-manager"
import * as path from "path"

export interface CognitoStackProps extends cdk.NestedStackProps {
  config: AppConfig
  callbackUrls?: string[]
}

export class CognitoStack extends cdk.NestedStack {
  public userPoolId: string
  public userPoolClientId: string
  public userPoolDomain: cognito.UserPoolDomain
  // Exposed as ARN string (not the Function object) so BackendStack can import
  // the Lambda and grant DynamoDB read access without creating a circular
  // nested-stack dependency. BackendStack uses lambda.Function.fromFunctionArn()
  // to get an IFunction reference scoped to BackendStack, then calls grantReadData.
  public preSignupLambdaArn: string

  constructor(scope: Construct, id: string, props: CognitoStackProps) {
    super(scope, id, props)

    this.createCognitoUserPool(props.config, props.callbackUrls)
  }

  private createCognitoUserPool(config: AppConfig, callbackUrls?: string[]): void {
    const defaultCallbackUrls = ["http://localhost:3000", "https://localhost:3000"]
    const finalCallbackUrls = callbackUrls || defaultCallbackUrls

    // ── PRE-SIGNUP LAMBDA TRIGGER ─────────────────────────────────────────────
    // Fires synchronously BEFORE a new Cognito account is created.
    // Blocks sign-up unless the registering email has a valid PENDING unexpired
    // invite in the invites table — enforcing invite-only external onboarding.
    //
    // INVITES_TABLE_NAME env var is intentionally left empty here. It cannot be
    // set at this point because InvitesTable lives in BackendStack, which is
    // created AFTER CognitoStack in fast-main-stack.ts. fast-main-stack.ts calls
    // preSignupLambda.addEnvironment("INVITES_TABLE_NAME", ...) after both
    // stacks are instantiated, and also grants DynamoDB read access.
    //
    // Fail closed: if the Lambda raises an exception, Cognito blocks sign-up.
    // AdminCreateUser flows (internal/CDK users) fire PreSignUp_AdminCreateUser
    // which is skipped by the Lambda — internal users are never blocked.
    // Local variable used for trigger wiring below. ARN is exposed via
    // this.preSignupLambdaArn so BackendStack can import it without a circular dep.
    // INVITES_TABLE_NAME starts as placeholder — BackendStack calls addEnvironment
    // on the imported IFunction reference after the table is created.
    const preSignupLambda = new lambda.Function(this, "PreSignupLambda", {
      functionName: `${config.stack_name_base}-pre-signup`,
      runtime:      lambda.Runtime.PYTHON_3_13,
      handler:      "index.handler",
      code:         lambda.Code.fromAsset(
        path.join(__dirname, "..", "lambdas", "pre-signup")
      ),
      architecture: lambda.Architecture.ARM_64,
      timeout:      cdk.Duration.seconds(10),
      memorySize:   128,
      environment: {
        // SSM parameter path where BackendStack writes the invites table name.
        // The Lambda reads the actual table name from SSM at runtime — avoids
        // a circular nested-stack dependency (CognitoStack ↔ BackendStack).
        // BackendStack writes: /{stack_name_base}/rag/invites-table-name
        INVITES_TABLE_SSM_PARAM: `/${config.stack_name_base}/rag/invites-table-name`,
      },
      logGroup: new logs.LogGroup(this, "PreSignupLogGroup", {
        logGroupName:  `/aws/lambda/${config.stack_name_base}-pre-signup`,
        retention:     logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    })
    // Grant SSM read so the Lambda can fetch the invites table name at runtime.
    // Grant DynamoDB read on the invites table directly — table name follows a
    // known pattern so we can construct the ARN without a cross-stack reference.
    // Table name: ${stack_name_base}-org-invites (set in BackendStack).
    preSignupLambda.addToRolePolicy(new iam.PolicyStatement({
      effect:  iam.Effect.ALLOW,
      actions: ["ssm:GetParameter"],
      resources: [
        `arn:aws:ssm:*:*:parameter/${config.stack_name_base}/rag/invites-table-name`,
      ],
    }))
    preSignupLambda.addToRolePolicy(new iam.PolicyStatement({
      effect:  iam.Effect.ALLOW,
      actions: [
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:BatchGetItem",
        "dynamodb:ConditionCheckItem",
        "dynamodb:DescribeTable",
      ],
      // Table name is deterministic: set in BackendStack as `${stack_name_base}-org-invites`
      resources: [
        `arn:aws:dynamodb:*:*:table/${config.stack_name_base}-org-invites`,
        `arn:aws:dynamodb:*:*:table/${config.stack_name_base}-org-invites/index/*`,
      ],
    }))

    // Expose the ARN as a plain string — BackendStack imports this as IFunction
    this.preSignupLambdaArn = preSignupLambda.functionArn

    // ── POST-CONFIRMATION LAMBDA TRIGGER    ──────
    // Fires after a user self-registers and confirms their email.
    // Automatically adds them to the 'external' group (Company B / client users).
    //
    // INTERNAL users (Company A / vendor staff) are created directly by an admin
    // (via Cognito console, CLI, or config.admin_user_email below) and manually
    // placed in the 'internal' group. They never self-register.
    //
    // Fail-safe: if the trigger errors, the Lambda logs it but does NOT re-raise.
    // The sign-up completes. The role.py utility defaults ungrouped users to EXTERNAL.
    const postConfirmationLambda = new lambda.Function(this, "PostConfirmationLambda", {
      functionName: `${config.stack_name_base}-post-confirmation`,
      runtime:      lambda.Runtime.PYTHON_3_13,
      handler:      "index.handler",
      code:         lambda.Code.fromAsset(
        path.join(__dirname, "..", "lambdas", "post-confirmation")
      ),
      architecture: lambda.Architecture.ARM_64,
      timeout:      cdk.Duration.seconds(10),
      memorySize:   128,
      environment: {
        // USER_POOL_ID is intentionally NOT set here — reading it from the CDK
        // token (userPool.userPoolId) would create a circular dependency:
        //   UserPool → Lambda (LambdaConfig) ←→ Lambda → UserPool (USER_POOL_ID env)
        // The Lambda reads pool_id directly from event["userPoolId"] instead,
        // which Cognito always provides in the trigger event payload.
        EXTERNAL_GROUP_NAME: "external",
      },
      logGroup: new logs.LogGroup(this, "PostConfirmationLogGroup", {
        logGroupName:  `/aws/lambda/${config.stack_name_base}-post-confirmation`,
        retention:     logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    })

    const userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: `${config.stack_name_base}-user-pool`,
      // selfSignUpEnabled: true — allows Company B client users to self-register.
      // They land in the 'external' group via the post-confirmation trigger.
      selfSignUpEnabled: true,
      signInAliases: {
        email: true,
      },
      autoVerify: {
        email: true,
      },
      standardAttributes: {
        email: {
          required: true,
          mutable: false,
        },
      },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      // NOTE: lambdaTriggers intentionally NOT set here — see escape hatch below.

      // userVerification: email sent to self-registering users with their
      // 6-digit confirmation code. Required for selfSignUpEnabled: true.
      // Without this, Cognito Managed Login v2 throws:
      //   "User Pool not configured properly for confirmation code delivery."
      userVerification: {
        emailSubject: `Your ${config.stack_name_base} verification code`,
        emailBody:    `<p>Hello,</p>
<p>Your verification code for <strong>${config.stack_name_base}</strong> is: <strong>{####}</strong></p>
<p>Enter this code to complete your registration. The code expires in 24 hours.</p>
<p>If you did not request this, you can safely ignore this email.</p>
<p>Thanks,<br/>${config.stack_name_base} Team</p>`,
        emailStyle:   cognito.VerificationEmailStyle.CODE,
      },

      // userInvitation: email sent to admin-created users with their temp password.
      userInvitation: {
        emailSubject: `Welcome to ${config.stack_name_base}!`,
        emailBody: `<p>Hello {username},</p>
<p>Welcome to ${config.stack_name_base}! Your username is <strong>{username}</strong> and your temporary password is: <strong>{####}</strong></p>
<p>Please use this temporary password to log in and set your permanent password.</p>
<p>Thanks,</p>
<p>${config.stack_name_base} Team</p>`,
      },
    })

    // Grant the post-confirmation Lambda permission to call AdminAddUserToGroup.
    // Using "*" resource to avoid any UserPool ARN reference before the pool exists.
    postConfirmationLambda.addToRolePolicy(new iam.PolicyStatement({
      effect:    iam.Effect.ALLOW,
      actions:   ["cognito-idp:AdminAddUserToGroup"],
      resources: ["*"],
    }))

    // ── TRIGGER WIRING VIA CFN ESCAPE HATCH    ───
    //
    // Approach: set LambdaConfig.PostConfirmation directly on the CfnUserPool
    // resource via addPropertyOverride. This is a native CloudFormation property,
    // so it persists across every deploy — no Custom Resource re-run needed.
    //
    // Why this avoids the circular dependency:
    //   - CDK's lambdaTriggers / addTrigger auto-creates a Lambda::Permission CFN
    //     resource that references the UserPool ARN, creating a cycle.
    //   - Here, the Lambda permission is granted via AwsCustomResource (below),
    //     which is NOT a CFN Lambda::Permission resource. No cycle exists.
    //
    // The Lambda must be created before the UserPool so the ARN token can be
    // resolved. addDependency enforces this ordering.
    const cfnUserPool = userPool.node.defaultChild as cognito.CfnUserPool
    cfnUserPool.addPropertyOverride(
      "LambdaConfig.PostConfirmation",
      postConfirmationLambda.functionArn
    )
    // Wire pre-signup trigger — same escape hatch pattern as PostConfirmation.
    // PreSignUp fires before account creation; raising in the Lambda blocks sign-up.
    cfnUserPool.addPropertyOverride(
      "LambdaConfig.PreSignUp",
      preSignupLambda.functionArn
    )
    cfnUserPool.node.addDependency(postConfirmationLambda)
    cfnUserPool.node.addDependency(preSignupLambda)

    // ── LAMBDA INVOKE PERMISSION  ──────
    //
    // Grant cognito-idp.amazonaws.com permission to invoke the Lambda, scoped to
    // this user pool's ARN (SourceArn). SourceArn is required — SourceAccount
    // alone is insufficient and Cognito will silently skip the trigger.
    //
    // Uses AwsCustomResource (not CDK addPermission / Lambda::Permission) to keep
    // this out of the CloudFormation dependency graph and avoid circular refs.
    //
    // StatementId "AllowCognitoInvoke" — clean name, no collision with old IDs.
    // ignoreErrorCodesMatching: ResourceConflictException — safe on re-deploy.
    const permissionRole = new iam.Role(this, "TriggerWirerRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
      ],
      inlinePolicies: {
        TriggerWirer: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions:   ["lambda:AddPermission", "lambda:RemovePermission"],
              // Covers both trigger Lambdas — PostConfirmation and PreSignup
              resources: [
                postConfirmationLambda.functionArn,
                preSignupLambda.functionArn,
              ],
            }),
          ],
        }),
      },
    })

    const addPermission = new cr.AwsCustomResource(this, "PostConfirmationInvokePermissionV3", {
      role: permissionRole,
      onCreate: {
        service:    "Lambda",
        action:     "addPermission",
        parameters: {
          FunctionName: postConfirmationLambda.functionArn,
          StatementId:  "AllowCognitoInvoke",
          Action:       "lambda:InvokeFunction",
          Principal:    "cognito-idp.amazonaws.com",
          SourceArn:    userPool.userPoolArn,
        },
        physicalResourceId:       cr.PhysicalResourceId.of("PostConfirmationInvokePermissionV3"),
        ignoreErrorCodesMatching: "ResourceConflictException",
      },
      onDelete: {
        service:    "Lambda",
        action:     "removePermission",
        parameters: {
          FunctionName: postConfirmationLambda.functionArn,
          StatementId:  "AllowCognitoInvoke",
        },
        physicalResourceId:       cr.PhysicalResourceId.of("PostConfirmationInvokePermissionV3"),
        ignoreErrorCodesMatching: "ResourceNotFoundException",
      },
      installLatestAwsSdk: true,
    })

    addPermission.node.addDependency(postConfirmationLambda)
    addPermission.node.addDependency(userPool)

    // ── PRE-SIGNUP INVOKE PERMISSION ──────────────────────────────────────────
    // Same pattern as PostConfirmation invoke permission above.
    // StatementId must be unique per Lambda function — "AllowCognitoPreSignup".
    const preSignupPermission = new cr.AwsCustomResource(this, "PreSignupInvokePermission", {
      role: permissionRole,
      onCreate: {
        service:    "Lambda",
        action:     "addPermission",
        parameters: {
          FunctionName: preSignupLambda.functionArn,
          StatementId:  "AllowCognitoPreSignup",
          Action:       "lambda:InvokeFunction",
          Principal:    "cognito-idp.amazonaws.com",
          SourceArn:    userPool.userPoolArn,
        },
        physicalResourceId:       cr.PhysicalResourceId.of("PreSignupInvokePermission"),
        ignoreErrorCodesMatching: "ResourceConflictException",
      },
      onDelete: {
        service:    "Lambda",
        action:     "removePermission",
        parameters: {
          FunctionName: preSignupLambda.functionArn,
          StatementId:  "AllowCognitoPreSignup",
        },
        physicalResourceId:       cr.PhysicalResourceId.of("PreSignupInvokePermission"),
        ignoreErrorCodesMatching: "ResourceNotFoundException",
      },
      installLatestAwsSdk: true,
    })

    preSignupPermission.node.addDependency(preSignupLambda)
    preSignupPermission.node.addDependency(userPool)

    // ── COGNITO GROUPS    ──────
    //
    // 'internal' group (precedence 1 — higher priority):
    //   Company A / vendor staff. Full access: document library, collections,
    //   chunk previews, source viewer, debug panels.
    //   Assignment: admin creates users and manually assigns this group.
    //
    // 'external' group (precedence 10 — lower priority):
    //   Company B / client users. Chat-only interface. No knowledge structure visible.
    //   Assignment: auto-assigned by post-confirmation Lambda on self-registration.
    //
    // Precedence tie-break: if a user is in both groups (admin error),
    // role.py checks 'internal' first and returns INTERNAL. Fail open for
    // internal staff, not fail open for external access.
    const internalGroup = new cognito.CfnUserPoolGroup(this, "InternalGroup", {
      userPoolId:  userPool.userPoolId,
      groupName:   "internal",
      description: "Company A internal users — full document library and management access",
      precedence:  1,
    })

    const externalGroup = new cognito.CfnUserPoolGroup(this, "ExternalGroup", {
      userPoolId:  userPool.userPoolId,
      groupName:   "external",
      description: "Company B external client users — chat-only interface",
      precedence:  10,
    })

    // Groups must exist before the post-confirmation trigger could assign a user.
    // CDK does not automatically infer this dependency — set it explicitly.
    internalGroup.node.addDependency(userPool)
    externalGroup.node.addDependency(userPool)

    const userPoolClient = new cognito.UserPoolClient(this, "UserPoolClient", {
      userPool: userPool,
      userPoolClientName: `${config.stack_name_base}-client`,
      generateSecret: false,
      authFlows: {
        userPassword: true,
        userSrp: true,
      },
      oAuth: {
        flows: {
          authorizationCodeGrant: true,
        },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: finalCallbackUrls,
        logoutUrls: finalCallbackUrls,
      },
      preventUserExistenceErrors: true,
    })

    this.userPoolDomain = new cognito.UserPoolDomain(this, "UserPoolDomain", {
      userPool: userPool,
      cognitoDomain: {
        domainPrefix: `${config.stack_name_base.toLowerCase()}-${cdk.Aws.ACCOUNT_ID}-${
          cdk.Aws.REGION
        }`,
      },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    })

    const managedLoginBranding = new cognito.CfnManagedLoginBranding(this, "ManagedLoginBranding", {
      userPoolId: userPool.userPoolId,
      clientId: userPoolClient.userPoolClientId,
      useCognitoProvidedValues: true,
    })

    managedLoginBranding.node.addDependency(this.userPoolDomain)

    this.userPoolId = userPool.userPoolId
    this.userPoolClientId = userPoolClient.userPoolClientId

    // Create admin user if email is provided in config.
    // Admin-created users do NOT trigger PostConfirmation_ConfirmSignUp,
    // so the post-confirmation Lambda will NOT run for them. We assign
    // the 'internal' group explicitly via CfnUserPoolUserToGroupAttachment.
    if (config.admin_user_email) {
      const adminUser = new cognito.CfnUserPoolUser(this, "AdminUser", {
        userPoolId: userPool.userPoolId,
        username: config.admin_user_email,
        userAttributes: [
          {
            name: "email",
            value: config.admin_user_email,
          },
        ],
        desiredDeliveryMediums: ["EMAIL"],
      })

      // Assign admin user to 'internal' group explicitly.
      // CfnUserPoolUserToGroupAttachment ensures this happens after both
      // the user and the group are created.
      const adminGroupAssignment = new cognito.CfnUserPoolUserToGroupAttachment(
        this,
        "AdminUserInternalGroup",
        {
          userPoolId: userPool.userPoolId,
          username:   config.admin_user_email,
          groupName:  "internal",
        }
      )
      adminGroupAssignment.node.addDependency(adminUser)
      adminGroupAssignment.node.addDependency(internalGroup)

      new cdk.CfnOutput(this, "AdminUserCreated", {
        description: "Admin user created and credentials emailed",
        value: `Admin user created: ${config.admin_user_email}`,
      })
    }

    // Outputs for reference — useful when manually adding internal users via CLI
    new cdk.CfnOutput(this, "InternalGroupName", {
      description: "Cognito group name for internal (vendor) users — assign via: aws cognito-idp admin-add-user-to-group --user-pool-id <id> --username <email> --group-name internal",
      value: "internal",
    })

    new cdk.CfnOutput(this, "ExternalGroupName", {
      description: "Cognito group name for external (client) users — auto-assigned on self-registration",
      value: "external",
    })
  }
}
