import * as cdk from "aws-cdk-lib"
import * as amplify from "@aws-cdk/aws-amplify-alpha"
import * as s3 from "aws-cdk-lib/aws-s3"
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment"
import * as iam from "aws-cdk-lib/aws-iam"
import * as lambda from "aws-cdk-lib/aws-lambda"
import * as logs from "aws-cdk-lib/aws-logs"
import { Construct } from "constructs"
import { AppConfig } from "./utils/config-manager"
import * as path from "path"

export interface AmplifyStackProps extends cdk.NestedStackProps {
  config: AppConfig
  // All the values needed to generate aws-exports.json at deploy time
  cognitoAuthority: string
  cognitoClientId: string
  cognitoRedirectUri: string
  agentRuntimeArn: string
  awsRegion: string
  feedbackApiUrl: string
  docsApiUrl: string
  agentPattern: string
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

    // Generate aws-exports.json content from CDK values
    // This file is fetched by the React app at runtime (fetch("/aws-exports.json"))
    const awsExportsContent = JSON.stringify({
      authority:                    props.cognitoAuthority,
      client_id:                    props.cognitoClientId,
      redirect_uri:                 props.cognitoRedirectUri,
      post_logout_redirect_uri:     props.cognitoRedirectUri,
      response_type:                "code",
      scope:                        "email openid profile",
      automaticSilentRenew:         true,
      agentRuntimeArn:              props.agentRuntimeArn,
      awsRegion:                    props.awsRegion,
      feedbackApiUrl:               props.feedbackApiUrl,
      docsApiUrl:                   props.docsApiUrl,
      agentPattern:                 props.agentPattern,
    }, null, 2)

    // Build frontend zip + aws-exports.json and push to staging S3 bucket
    s3deploy.Source.asset(path.join(__dirname, "../../frontend"), {
  bundling: {
    image: cdk.DockerImage.fromRegistry("node:20-alpine"),
    command: [
      "sh", "-c",
      "npm ci && npm run build && cd build && zip -r /asset-output/frontend-build.zip ."
    ],
    // Runs on your machine if Docker is available, otherwise in Lambda
    local: {
      tryBundle(outputDir) {
        // Falls back to running npm locally if no Docker
        const result = require("child_process").spawnSync(
          "sh", ["-c", `cd ${path.join(__dirname, "../../frontend")} && npm run build && cd build && zip -r ${outputDir}/frontend-build.zip .`],
          { stdio: "inherit" }
        )
        return result.status === 0
      }
    }
  }
})

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
      code:         lambda.Code.fromAsset(
        path.join(__dirname, "..", "lambdas", "frontend-deployer")
      ),
      handler:      "index.handler",
      architecture: lambda.Architecture.ARM_64,

      timeout: cdk.Duration.minutes(5),
      logGroup: new logs.LogGroup(this, "FrontendDeployerLogGroup", {
        logGroupName:   `/aws/lambda/${props.config.stack_name_base}-frontend-deployer`,
        retention:      logs.RetentionDays.ONE_WEEK,
        removalPolicy:  cdk.RemovalPolicy.DESTROY,
      }),
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

    // Custom Resource — triggers the deployer Lambda on every cdk deploy
    const deployerResource = new cdk.CustomResource(this, "FrontendDeployerResource", {
      serviceToken: deployerLambda.functionArn,
      properties: {
        AppId:          this.amplifyApp.appId,
        BranchName:     "main",
        StagingBucket:  this.stagingBucket.bucketName,
        ZipKey:         "frontend/frontend-build.zip",
        // Changing DeployVersion forces a re-deploy on next cdk deploy.
        // Update this value whenever you want to push new frontend code.
        DeployVersion:  "1",
      },
    })

    deployerResource.node.addDependency(this.stagingBucket)

    new cdk.CfnOutput(this, "AmplifyUrl", {
      value:       this.amplifyUrl,
      description: "Live frontend URL",
    })
  }
}