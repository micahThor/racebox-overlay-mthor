"""Everything the club web app runs on.

    people ──HTTPS──> CloudFront ──> EC2 instance (Docker: racebox-overlay serve)
                         │  ├─ sign-in: Cognito (only people you add)
                         │  └─ when the instance is asleep: a "waking up" page that
                         │     calls a small Lambda to start it again
                         └──> S3: finished videos to download (deleted after keepRendersMinutes)

One EC2 instance does everything (editor, previews, renders, one at a time). It
stops itself after idleMinutes with nobody using it, and you pay only for its
disk while it's stopped. Uploaded videos and settings live on a separate EBS
volume, so they survive the instance being stopped or replaced.
"""
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration, Fn, RemovalPolicy, Stack, Tags,
    aws_budgets as budgets,
    aws_events as events,
    aws_events_targets as targets,
    aws_cloudfront as cf,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_ssm as ssm,
    custom_resources as cr,
)
from constructs import Construct

HERE = Path(__file__).resolve().parent.parent
# the app's image: built and published by GitHub Actions (no AWS sign-in needed there)
DEFAULT_IMAGE = "ghcr.io/micahthor/racebox-overlay-mthor:latest"
CONFIG_PARAM = "/racebox-overlay/config"
PORT = 8080
TAG = ("racebox", "server")


def ctx(scope: Construct, name: str, default=None):
    v = scope.node.try_get_context(name)
    return default if v is None or v == "" else v


class ServerStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw) -> None:
        super().__init__(scope, cid, **kw)
        region = self.region
        if cdk.Token.is_unresolved(region):
            raise ValueError("set the AWS region (AWS_REGION or your AWS profile) before cdk synth/deploy")

        # ------------------------------------------------------------ network
        vpc = ec2.Vpc(self, "Vpc", max_azs=1, nat_gateways=0,
                      subnet_configuration=[ec2.SubnetConfiguration(
                          name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)])
        sg = ec2.SecurityGroup(self, "ServerSg", vpc=vpc, allow_all_outbound=True,
                               description="Only CloudFront can reach the editor")
        # CloudFront's address list, looked up while deploying
        cf_list = cr.AwsCustomResource(
            self, "CloudFrontPrefixList",
            on_update=cr.AwsSdkCall(
                service="EC2", action="describeManagedPrefixLists",
                parameters={"Filters": [{"Name": "prefix-list-name",
                                         "Values": ["com.amazonaws.global.cloudfront.origin-facing"]}]},
                physical_resource_id=cr.PhysicalResourceId.of("cloudfront-origin-facing"),
                output_paths=["PrefixLists.0.PrefixListId"]),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE),
            install_latest_aws_sdk=False)
        sg.add_ingress_rule(ec2.Peer.prefix_list(cf_list.get_response_field("PrefixLists.0.PrefixListId")),
                            ec2.Port.tcp(PORT), "CloudFront to the editor")

        # ------------------------------------------------------------ storage
        renders = s3.Bucket(
            self, "Renders", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            # renders go after keepRendersMinutes (the cleanup function below); this is a backstop
            lifecycle_rules=[s3.LifecycleRule(prefix="outputs/", expiration=Duration.days(1)),
                             s3.LifecycleRule(abort_incomplete_multipart_upload_after=Duration.days(2))])
        site = s3.Bucket(self, "WakePage", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                         encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True,
                         removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)

        # ------------------------------------------------------------ the server
        role = iam.Role(self, "ServerRole", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
                        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                            "AmazonSSMManagedInstanceCore")])         # Session Manager instead of SSH
        renders.grant_read_write(role)
        renders.grant_delete(role)
        role.add_to_policy(iam.PolicyStatement(actions=["polly:SynthesizeSpeech"], resources=["*"]))
        role.add_to_policy(iam.PolicyStatement(          # read its settings (by name: no circular references)
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{region}:{self.account}:parameter{CONFIG_PARAM}"]))
        role.add_to_policy(iam.PolicyStatement(          # stop itself when nobody's using it
            actions=["ec2:StopInstances"], resources=[f"arn:aws:ec2:{region}:{self.account}:instance/*"],
            conditions={"StringEquals": {f"aws:ResourceTag/{TAG[0]}": TAG[1]}}))

        data = ec2.Volume(self, "Data", availability_zone=vpc.public_subnets[0].availability_zone,
                          size=cdk.Size.gibibytes(int(ctx(self, "dataGb", 100))),
                          volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True,
                          removal_policy=RemovalPolicy.SNAPSHOT)

        image = ctx(self, "image", DEFAULT_IMAGE)
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(*(HERE / "server_setup.sh").read_text().replace(
            "__VOLUME_ID__", data.volume_id).replace("__IMAGE__", image).replace(
            "__REGION__", region).replace("__CONFIG_PARAM__", CONFIG_PARAM).replace(
            "__PORT__", str(PORT)).splitlines())

        server = ec2.Instance(
            self, "Server", vpc=vpc, vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            instance_type=ec2.InstanceType(ctx(self, "instanceType", "c7i.2xlarge")),
            # looked up each time it starts, so a new Amazon Linux doesn't replace the server
            machine_image=ec2.MachineImage.resolve_ssm_parameter_at_launch(
                "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"),
            role=role, security_group=sg, user_data=user_data, require_imdsv2=True,
            block_devices=[ec2.BlockDevice(device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(
                30, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True))])
        Tags.of(server).add(*TAG)
        ec2.CfnVolumeAttachment(self, "DataAttach", instance_id=server.instance_id,
                                volume_id=data.volume_id, device="/dev/sdf")
        ip = ec2.CfnEIP(self, "Ip", domain="vpc", instance_id=server.instance_id)
        dns_suffix = "compute-1.amazonaws.com" if region == "us-east-1" else f"{region}.compute.amazonaws.com"
        origin_dns = Fn.join("", ["ec2-", Fn.join("-", Fn.split(".", ip.attr_public_ip)), ".", dns_suffix])

        # ------------------------------------------------------------ waking it up
        wake = lambda_.Function(
            self, "Wake", runtime=lambda_.Runtime.PYTHON_3_12, handler="index.handler",
            timeout=Duration.seconds(15), memory_size=128,
            code=lambda_.Code.from_inline((HERE / "wake" / "wake.py").read_text()),
            environment={"INSTANCE_ID": server.instance_id},
            description="Starts the RaceBox Overlay server when someone opens the editor")
        wake.add_to_role_policy(iam.PolicyStatement(actions=["ec2:DescribeInstances"], resources=["*"]))
        wake.add_to_role_policy(iam.PolicyStatement(
            actions=["ec2:StartInstances"],
            resources=[f"arn:aws:ec2:{region}:{self.account}:instance/{server.instance_id}"]))
        wake_url = wake.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)

        # ------------------------------------------------------------ clearing old renders
        # every 5 minutes, even while the server sleeps (S3's own expiry works in whole days)
        keep_minutes = int(ctx(self, "keepRendersMinutes", 60))
        cleanup = lambda_.Function(
            self, "RenderCleanup", runtime=lambda_.Runtime.PYTHON_3_12, handler="index.handler",
            timeout=Duration.seconds(60), memory_size=128,
            code=lambda_.Code.from_inline((HERE / "wake" / "cleanup.py").read_text()),
            environment={"BUCKET": renders.bucket_name, "KEEP_MINUTES": str(keep_minutes)},
            description="Deletes rendered videos once they're older than the limit")
        renders.grant_read(cleanup)
        renders.grant_delete(cleanup)
        events.Rule(self, "RenderCleanupSchedule", schedule=events.Schedule.rate(Duration.minutes(5)),
                    targets=[targets.LambdaFunction(cleanup)])

        # ------------------------------------------------------------ CloudFront
        no_cache = cf.CachePolicy.CACHING_DISABLED
        app_origin = origins.HttpOrigin(
            origin_dns, protocol_policy=cf.OriginProtocolPolicy.HTTP_ONLY, http_port=PORT,
            read_timeout=Duration.seconds(60), keepalive_timeout=Duration.seconds(60),
            connection_attempts=1, connection_timeout=Duration.seconds(4))
        dist = cf.Distribution(
            self, "Cdn", comment="RaceBox Overlay editor",
            price_class=cf.PriceClass.PRICE_CLASS_100,
            default_behavior=cf.BehaviorOptions(
                origin=app_origin, allowed_methods=cf.AllowedMethods.ALLOW_ALL,
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=no_cache, origin_request_policy=cf.OriginRequestPolicy.ALL_VIEWER,
                compress=True),
            additional_behaviors={
                "/_rbo/*": cf.BehaviorOptions(
                    origin=origins.S3BucketOrigin.with_origin_access_control(site),
                    viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=no_cache),
                "/_wake": cf.BehaviorOptions(
                    origin=origins.FunctionUrlOrigin.with_origin_access_control(wake_url),
                    viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=no_cache,
                    origin_request_policy=cf.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER),
            },
            # the server is asleep (or still starting): show the waking-up page
            error_responses=[cf.ErrorResponse(http_status=code, response_http_status=503,
                                              response_page_path="/_rbo/wake.html", ttl=Duration.seconds(0))
                             for code in (502, 503, 504)])
        s3deploy.BucketDeployment(self, "WakePageFiles", destination_bucket=site,
                                  destination_key_prefix="_rbo/",
                                  sources=[s3deploy.Source.asset(str(HERE / "wake"), exclude=["*.py"])],
                                  prune=True)
        url = f"https://{dist.distribution_domain_name}"

        # ------------------------------------------------------------ sign-in
        pool = cognito.UserPool(
            self, "Users", user_pool_name="racebox-overlay", self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            password_policy=cognito.PasswordPolicy(min_length=10, require_symbols=False),
            user_invitation=cognito.UserInvitationConfig(
                email_subject="You're invited to the RaceBox Overlay editor",
                email_body=("You've been added to the club's RaceBox Overlay editor.<br><br>"
                            f"Open <a href=\"{url}\">{url}</a> and sign in with<br>"
                            "email: {username}<br>temporary password: {####}<br><br>"
                            "You'll choose your own password the first time you sign in.")),
            removal_policy=RemovalPolicy.RETAIN)
        prefix = ctx(self, "authDomainPrefix") or Fn.join("-", ["racebox-overlay", self.account])
        domain = pool.add_domain("SignInDomain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=prefix))
        client = pool.add_client(
            "Editor", generate_secret=False, prevent_user_existence_errors=True,
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            id_token_validity=Duration.hours(1),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{url}/auth/callback"], logout_urls=[f"{url}/"]))

        # ------------------------------------------------------------ the app's settings
        ssm.StringParameter(
            self, "Config", parameter_name=CONFIG_PARAM, description="racebox-overlay serve settings",
            string_value=Stack.of(self).to_json_string({
                "public_url": url, "region": region, "bucket": renders.bucket_name,
                "cognito_domain": domain.base_url(), "cognito_client_id": client.user_pool_client_id,
                "cognito_pool_id": pool.user_pool_id, "auth": "cognito", "tts": "polly",
                "idle_minutes": str(ctx(self, "idleMinutes", 30)),
                "max_gb": str(ctx(self, "maxUploadGb", 20)),
                "keep_renders_minutes": str(keep_minutes), "cleanup_by_bucket": "1",
                "keep_uploads_hours": str(ctx(self, "keepUploadsHours", 72))}))

        # ------------------------------------------------------------ a spending alarm
        budget_email = ctx(self, "budgetEmail")
        if budget_email:
            budgets.CfnBudget(self, "Budget", budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST", time_unit="MONTHLY", budget_name="racebox-overlay",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=float(ctx(self, "monthlyBudgetUsd", 60)), unit="USD")),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            notification_type=kind, comparison_operator="GREATER_THAN",
                            threshold=80, threshold_type="PERCENTAGE"),
                        subscribers=[budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL", address=budget_email)])
                    for kind in ("ACTUAL", "FORECASTED")])

        cdk.CfnOutput(self, "EditorUrl", value=url, description="Open this to use the editor")
        cdk.CfnOutput(self, "InstanceId", value=server.instance_id)
        cdk.CfnOutput(self, "UserPoolId", value=pool.user_pool_id,
                      description="Add club members here (see docs/aws.md)")
        cdk.CfnOutput(self, "RendersBucket", value=renders.bucket_name)
