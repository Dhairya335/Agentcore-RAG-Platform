import * as cdk from "aws-cdk-lib"
import { Construct } from "constructs"
import { AppConfig } from "./utils/config-manager"

// Import nested stacks
import { BackendStack } from "./backend-stack"
import { AmplifyHostingStack } from "./amplify-hosting-stack"
import { CognitoStack } from "./cognito-stack"

export interface FastAmplifyStackProps extends cdk.StackProps {
  config: AppConfig
}

export class FastMainStack extends cdk.Stack {
  public readonly amplifyHostingStack: AmplifyHostingStack
  public readonly backendStack: BackendStack
  public readonly cognitoStack: CognitoStack

  constructor(scope: Construct, id: string, props: FastAmplifyStackProps) {
    const description =
      "Fullstack AgentCore Solution Template - Main Stack (v0.3.1) (uksb-v6dos0t5g8)"
    super(scope, id, { ...props, description })

    // Step 1: Create Amplify stack first to get the predictable domain URL.
    // Need the URL before Cognito so we can add it as a callback URL.
    // NOTE: At this point AmplifyHostingStack gets a placeholder for values that come from Cognito/Backend — those are resolved by CFN at deploy time.
    this.amplifyHostingStack = new AmplifyHostingStack(this, `${id}-amplify`, {
      config: props.config,
      // Cognito values — resolved by CFN token substitution at deploy time
      cognitoAuthority:   `https://cognito-idp.${cdk.Aws.REGION}.amazonaws.com/${cdk.Lazy.string({
        produce: () => this.cognitoStack.userPoolId
      })}`,
      cognitoClientId:    cdk.Lazy.string({
        produce: () => this.cognitoStack.userPoolClientId
      }),
      cognitoRedirectUri: `https://main.${this.amplifyHostingStack.amplifyApp.appId}.amplifyapp.com`,
      // Backend values — resolved after backend stack creates them
      agentRuntimeArn:    cdk.Lazy.string({
        produce: () => this.backendStack.runtimeArn
      }),
      awsRegion:          cdk.Aws.REGION,
      feedbackApiUrl:     cdk.Lazy.string({
        produce: () => this.backendStack.feedbackApiUrl
      }),
      docsApiUrl:         cdk.Lazy.string({
        produce: () => this.backendStack.docsApiUrl
      }),
      agentPattern:       props.config.backend?.pattern || "strands-single-agent",
    })

    // Step 2: Cognito — needs Amplify URL for callback URLs
    this.cognitoStack = new CognitoStack(this, `${id}-cognito`, {
      config: props.config,
      callbackUrls: ["http://localhost:3000", this.amplifyHostingStack.amplifyUrl],
    })

    // Step 3: Backend — needs Cognito IDs and Amplify URL
    this.backendStack = new BackendStack(this, `${id}-backend`, {
      config: props.config,
      userPoolId:       this.cognitoStack.userPoolId,
      userPoolClientId: this.cognitoStack.userPoolClientId,
      userPoolDomain:   this.cognitoStack.userPoolDomain,
      frontendUrl:      this.amplifyHostingStack.amplifyUrl,
    })

    // ── Outputs ────────────────────────────────────────────────────────────────

    new cdk.CfnOutput(this, "AmplifyAppId", {
      value:       this.amplifyHostingStack.amplifyApp.appId,
      description: "Amplify App ID",
      exportName:  `${props.config.stack_name_base}-AmplifyAppId`,
    })

    new cdk.CfnOutput(this, "CognitoUserPoolId", {
      value:       this.cognitoStack.userPoolId,
      description: "Cognito User Pool ID",
      exportName:  `${props.config.stack_name_base}-CognitoUserPoolId`,
    })

    new cdk.CfnOutput(this, "CognitoClientId", {
      value:       this.cognitoStack.userPoolClientId,
      description: "Cognito User Pool Client ID",
      exportName:  `${props.config.stack_name_base}-CognitoClientId`,
    })

    new cdk.CfnOutput(this, "CognitoDomain", {
      value:       `${this.cognitoStack.userPoolDomain.domainName}.auth.${cdk.Aws.REGION}.amazoncognito.com`,
      description: "Cognito Domain for OAuth",
      exportName:  `${props.config.stack_name_base}-CognitoDomain`,
    })

    new cdk.CfnOutput(this, "RuntimeArn", {
      value:       this.backendStack.runtimeArn,
      description: "AgentCore Runtime ARN",
      exportName:  `${props.config.stack_name_base}-RuntimeArn`,
    })

    new cdk.CfnOutput(this, "MemoryArn", {
      value:       this.backendStack.memoryArn,
      description: "AgentCore Memory ARN",
      exportName:  `${props.config.stack_name_base}-MemoryArn`,
    })

    new cdk.CfnOutput(this, "FeedbackApiUrl", {
      value:       this.backendStack.feedbackApiUrl,
      description: "Feedback API Gateway URL",
      exportName:  `${props.config.stack_name_base}-FeedbackApiUrl`,
    })

    new cdk.CfnOutput(this, "AmplifyConsoleUrl", {
      value:       `https://console.aws.amazon.com/amplify/apps/${this.amplifyHostingStack.amplifyApp.appId}`,
      description: "Amplify Console URL for monitoring deployments",
    })

    new cdk.CfnOutput(this, "AmplifyUrl", {
      value:       this.amplifyHostingStack.amplifyUrl,
      description: "Amplify Frontend URL",
    })

    new cdk.CfnOutput(this, "StagingBucketName", {
      value:       this.amplifyHostingStack.stagingBucket.bucketName,
      description: "S3 bucket for Amplify deployment staging",
      exportName:  `${props.config.stack_name_base}-StagingBucket`,
    })
  }
}