import * as cdk from "aws-cdk-lib"
import * as cognito from "aws-cdk-lib/aws-cognito"
import * as lambda from "aws-cdk-lib/aws-lambda"
import * as iam from "aws-cdk-lib/aws-iam"
import * as logs from "aws-cdk-lib/aws-logs"
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

  constructor(scope: Construct, id: string, props: CognitoStackProps) {
    super(scope, id, props)

    this.createCognitoUserPool(props.config, props.callbackUrls)
  }

  private createCognitoUserPool(config: AppConfig, callbackUrls?: string[]): void {
    const defaultCallbackUrls = ["http://localhost:3000", "https://localhost:3000"]
    const finalCallbackUrls = callbackUrls || defaultCallbackUrls

    // ── POST-CONFIRMATION LAMBDA TRIGGER ──────────────────────────────────────
    // Fires after a user self-registers and confirms their email.
    // Automatically adds them to the 'external' group (Company B / client users).
    //
    // INTERNAL users (Company A / vendor staff) are created directly by an admin
    // (via Cognito console, CLI, or config.admin_user_email below) and manually
    // placed in the 'internal' group. They never self-register.
    //
    // Fail-safe: if the trigger errors, the Lambda logs it but does NOT re-raise.
    // The sign-up completes. The role.py utility defaults ungroued users to EXTERNAL.
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
        // USER_POOL_ID cannot be set here because the pool doesn't exist yet.
        // It is injected below via addEnvironment after pool creation.
        USER_POOL_ID:        "PLACEHOLDER",
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
      // Wire the post-confirmation trigger
      lambdaTriggers: {
        postConfirmation: postConfirmationLambda,
      },
      userInvitation: {
        emailSubject: `Welcome to ${config.stack_name_base}!`,
        emailBody: `<p>Hello {username},</p>
<p>Welcome to ${config.stack_name_base}! Your username is <strong>{username}</strong> and your temporary password is: <strong>{####}</strong></p>
<p>Please use this temporary password to log in and set your permanent password.</p>
<p>The CloudFront URL to your application is stored as an output in the "${config.stack_name_base}" stack, and will be printed to your terminal once the deployment process completes.</p>
<p>Thanks,</p>
<p>Fullstack AgentCore Solution Template Team</p>`,
      },
    })

    // Inject the real User Pool ID now that the pool object exists.
    // CDK resolves this as a CloudFormation token reference — not a hardcoded string.
    postConfirmationLambda.addEnvironment("USER_POOL_ID", userPool.userPoolId)

    // Grant the post-confirmation Lambda permission to call AdminAddUserToGroup.
    // Without this, the trigger fires but throws AccessDeniedException — silent
    // to the user but logged in CloudWatch.
    //
    // NOTE: Using "*" instead of userPool.userPoolArn intentionally.
    // Referencing userPool.userPoolArn here would create a circular dependency:
    //   PostConfirmationLambda → IAM Policy → UserPool ARN → UserPool
    //   UserPool → lambdaTriggers → PostConfirmationLambda
    // CloudFormation cannot resolve this ordering. Using "*" breaks the cycle.
    // The Lambda is scoped to the correct pool via its USER_POOL_ID env var at runtime.
    postConfirmationLambda.addToRolePolicy(new iam.PolicyStatement({
      effect:    iam.Effect.ALLOW,
      actions:   ["cognito-idp:AdminAddUserToGroup"],
      resources: ["*"],
    }))

    // ── COGNITO GROUPS ────────────────────────────────────────────────────────
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
