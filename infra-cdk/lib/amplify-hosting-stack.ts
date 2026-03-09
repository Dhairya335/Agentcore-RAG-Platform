import * as cdk from "aws-cdk-lib"
import * as amplify from "@aws-cdk/aws-amplify-alpha"
import * as s3 from "aws-cdk-lib/aws-s3"
import * as iam from "aws-cdk-lib/aws-iam"
import * as lambda from "aws-cdk-lib/aws-lambda"
import { Construct } from "constructs"
import { AppConfig } from "./utils/config-manager"
import * as path from "path"
import * as logs from "aws-cdk-lib/aws-logs"

export interface AmplifyStackProps extends cdk.NestedStackProps {
  config: AppConfig
  // No cross-stack props here — eliminates circular dependency between
  // amplify/cognito/backend stacks. The deployer Lambda reads all runtime
  // values (Cognito IDs, API URLs etc.) from SSM at deploy time instead.
}

export class AmplifyHostingStack extends cdk.NestedStack {
  public readonly amplifyApp: amplify.App
  public readonly amplifyUrl: string
  public readonly stagingBucket: s3.Bucket

  constructor(scope: Construct, id: string, props: AmplifyStackProps) {
    const description = "Fullstack AgentCore Solution Template - Amplify Hosting Stack"
    super(scope, id, { ...props, description })

    // STAGING BUCKET — holds the built frontend zip for Amplify to consume
    const accessLogsBucket = new s3.Bucket(this, "StagingBucketAccessLogs", {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      publicReadAccess: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      lifecycleRules: [
        {
          id: "DeleteOldAccessLogs",
          enabled: true,
          expiration: cdk.Duration.days(90),
        },
      ],
    })

    this.stagingBucket = new s3.Bucket(this, "StagingBucket", {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      versioned: true,
      publicReadAccess: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: "staging-bucket-access-logs/",
      lifecycleRules: [
        {
          id: "DeleteOldDeployments",
          enabled: true,
          expiration: cdk.Duration.days(30),
        },
      ],
    })

    // Allow Amplify service to read from staging bucket
    this.stagingBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: "AmplifyAccess",
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal("amplify.amazonaws.com")],
        actions: ["s3:GetObject", "s3:GetObjectVersion"],
        resources: [this.stagingBucket.arnForObjects("*")],
      })
    )

    // Enforce SSL
    this.stagingBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: "DenyInsecureConnections",
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ["s3:*"],
        resources: [
          this.stagingBucket.bucketArn,
          this.stagingBucket.arnForObjects("*"),
        ],
        conditions: {
          Bool: { "aws:SecureTransport": "false" },
        },
      })
    )

    this.amplifyApp = new amplify.App(this, "AmplifyApp", {
      appName: `${props.config.stack_name_base}-frontend`,
      description: `${props.config.stack_name_base} - React Frontend`,
      platform: amplify.Platform.WEB,
    })

    this.amplifyApp.addBranch("main", {
      stage: "PRODUCTION",
      branchName: "main",
    })

    this.amplifyUrl = `https://main.${this.amplifyApp.appId}.amplifyapp.com`

    // Upload pre-built frontend zip to staging S3.
    //
    // The zip is built BEFORE cdk deploy via: make build-frontend
    // This produces infra-cdk/frontend-build.zip which CDK uploads as a plain asset.
    //
    // aws-exports.json is NOT generated here — it is assembled by the deployer
    // Lambda at deploy time by reading values from SSM. This avoids circular
    // dependencies between the amplify/cognito/backend nested stacks.
    const frontendZipPath = path.join(__dirname, "..", "frontend-build.zip")

    // Fail fast at synth time if zip is missing — tells you exactly what to run
    const fs = require("fs")
    if (!fs.existsSync(frontendZipPath)) {
      throw new Error(
        `\nfrontend-build.zip not found. Build it first:\n` +
        `  cd infra-cdk && make build-frontend\n`
      )
    }

    // Frontend Deployer Lambda + Custom Resource
    // WHY a Custom Resource Lambda:
    //   After the zip is in S3, need tocall Amplify's API to
    //   trigger a deployment. CloudFormation can't do this natively.
    //   A Custom Resource Lambda runs during cdk deploy and:
    //     1. Calls amplify.create_deployment() → gets a presigned upload URL
    //     2. Downloads the zip from staging S3
    //     3. Uploads it to Amplify via the presigned URL
    //     4. Calls amplify.start_deployment() → Amplify serves the new files
    //
    // This Lambda runs on every cdk deploy (Create + Update events), so frontend is always in sync with infrastructure.

    const deployerLambda = new lambda.Function(this, "FrontendDeployerLambda", {
      functionName: `${props.config.stack_name_base}-frontend-deployer`,
      runtime:      lambda.Runtime.PYTHON_3_13,
      code:         lambda.Code.fromAsset(...),
      handler:      "index.handler",
      architecture: lambda.Architecture.ARM_64,
      timeout:      cdk.Duration.minutes(5),
      logGroup: logs.LogGroup.fromLogGroupName(
        this,
        "FrontendDeployerLambdaLogGroup",
        `/aws/lambda/${props.config.stack_name_base}-frontend-deployer`
      ),
    })

    this.stagingBucket.grantRead(deployerLambda)

    deployerLambda.addToRolePolicy(new iam.PolicyStatement({
      effect:    iam.Effect.ALLOW,
      actions: [
        "amplify:CreateDeployment",
        "amplify:StartDeployment",
      ],
      resources: [
        `arn:aws:amplify:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}`
          + `:apps/${this.amplifyApp.appId}/branches/main/deployments`,
        `arn:aws:amplify:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}`
          + `:apps/${this.amplifyApp.appId}/branches/main/deployments/*`,
      ],
    }))

    // SSM read — Lambda reads Cognito/API values to build aws-exports.json
    deployerLambda.addToRolePolicy(new iam.PolicyStatement({
      effect:  iam.Effect.ALLOW,
      actions: ["ssm:GetParameter", "ssm:GetParameters"],
      resources: [
        `arn:aws:ssm:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}`
          + `:parameter/${props.config.stack_name_base}/*`,
      ],
    }))

    // Custom Resource — triggers the deployer Lambda on every cdk deploy
    const deployerResource = new cdk.CustomResource(this, "FrontendDeployerResource", {
      serviceToken: deployerLambda.functionArn,
      properties: {
        AppId:          this.amplifyApp.appId,
        BranchName:     "main",
        StagingBucket:  this.stagingBucket.bucketName,
        ZipKey:         "frontend/frontend-build.zip",
        StackName:      props.config.stack_name_base,
        AmplifyUrl:     this.amplifyUrl,
        DeployVersion:  "2",
      },
    })

    deployerResource.node.addDependency(this.stagingBucket)

    new cdk.CfnOutput(this, "AmplifyUrl", {
      value:       this.amplifyUrl,
      description: "Live frontend URL",
    })
  }
}