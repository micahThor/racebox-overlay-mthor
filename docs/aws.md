# The editor on the web, for your club (AWS)

The same editor you run with `racebox-overlay gui`, on a server your club signs in to.
Each person has their own videos, RaceBox files and saved layouts. Renders take turns
on the server, and finished videos download from the editor.

```
club members ──HTTPS──> CloudFront ──> one EC2 server (Docker: racebox-overlay serve)
                           │   ├─ sign-in: Amazon Cognito (only people you add)
                           │   └─ asleep? a "waking up" page starts it (Lambda)
                           └─> S3: finished videos (deleted after 30 days)
```

The server **goes to sleep after 30 minutes with nobody using it**. Opening the
editor's address wakes it up, which takes about two minutes. While it sleeps you pay
only for its disk.

Everything AWS is in `infra/` (CDK, Python). GitHub Actions checks every push, and
deploys when you ask it to (`.github/workflows/deploy.yml`, see "Deploying changes" below).

## What it costs (us-east-1, roughly)

| | |
|---|---|
| Server, c7i.2xlarge (8 vCPU) | about $0.36 an hour while it's awake; nothing while asleep |
| Disk for videos, 200 GB | about $16 a month |
| Server's address (public IPv4) | about $3.60 a month |
| CloudFront, S3, Cognito, Polly, Lambda | usually cents (free tiers cover a small club) |

A club that has it awake 40 hours a month pays about **$35 a month**. The biggest lever
is the disk: delete old videos, or make it smaller (`dataGb`). For renders 2x faster, use
`c7i.4xlarge` (about $0.71 an hour).

## Setting it up (once, from your computer)

You need an AWS account, the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
signed in as an administrator (`aws sts get-caller-identity` works), Node.js 18+ and Python 3.9+.

1. **Get the CDK tools** (from the repository's folder):

   ```sh
   cd infra
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   npm install -g aws-cdk
   ```

2. **Prepare the account for CDK** (once per account and region):

   ```sh
   export AWS_REGION=us-east-1
   cdk bootstrap
   ```

3. **Let GitHub deploy.** This creates a role that only this repository's `main` branch
   can use. It stores no AWS keys in GitHub.

   ```sh
   cdk deploy RaceboxGithub
   ```

   If your account already has a GitHub sign-in provider (the deploy says
   `token.actions.githubusercontent.com already exists`), pass its ARN instead:
   `cdk deploy RaceboxGithub -c githubOidcProviderArn=arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com`

   **If it fails with `iam:CreateOpenIDConnectProvider ... explicit deny in a service
   control policy`:** some new AWS accounts aren't allowed to add GitHub as a sign-in
   provider. Use a deploy user with an access key instead:

   ```sh
   cdk deploy RaceboxGithub -c githubAuth=keys
   ```

   Then in the console go to IAM → Users → `racebox-overlay-github-deploy` → Security
   credentials → **Create access key** → "Third-party service". In step 4, instead of
   `AWS_ROLE_ARN`, add the secrets `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` and the
   variable `AWS_DEPLOY_WITH_KEYS` = `true`. That user can only deploy this app.

4. **Tell GitHub about it.** In the repository on GitHub, go to Settings → Secrets and
   variables → Actions:
   - Variables tab:
     - `AWS_ROLE_ARN`: the `DeployRoleArn` the last step printed.
     - `AWS_REGION`: your region, for example `us-east-1`.
     - Optionally `INSTANCE_TYPE`, for example `c7i.4xlarge`.
   - Secrets tab. Use secrets, not variables, because this repository's Actions logs are public:
     - `ADMIN_EMAIL`: your email. You become the first person who can sign in.
     - Optionally `BUDGET_EMAIL`: this gets an email when the month's AWS bill heads past
       $60 (change the amount with `monthlyBudgetUsd` in `infra/cdk.json`).

5. **Deploy.** In the Actions tab, choose **Test and deploy**, then **Run workflow** (branch
   `main`, "Deploy to AWS" ticked). The first
   deploy takes about 15 minutes (CloudFront is slow to create). When it's done, the run's
   summary shows the editor's address. You can also find it later in the AWS console:
   CloudFormation → RaceboxServer → Outputs → `EditorUrl`.

6. **Sign in.** Cognito emails `ADMIN_EMAIL` a temporary password. Open the editor's
   address, sign in, and choose your own password.

## Deploying changes

Pushing code never deploys. Each push runs the checks (tests, the Docker image, the
AWS setup), which is free for a public repository, so you can push as often as you like.
When you want the club's server to have your changes, go to Actions → **Test and deploy**
→ **Run workflow**, leave "Deploy to AWS" ticked, and run it on `main`. It runs the same
checks first and deploys only if they pass.

From the command line: `gh workflow run deploy.yml --ref main`.

## Adding club members

Nobody can sign up on their own. To add someone:

- **In the console:** Cognito → User pools → `racebox-overlay` → Users → Create user.
  Choose "Send an email invitation", enter their email address, and tick "Mark email
  address as verified".
- **From the command line:**

  ```sh
  POOL=$(aws cloudformation describe-stacks --stack-name RaceboxServer \
         --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
  aws cognito-idp admin-create-user --user-pool-id "$POOL" --username rider@example.com \
      --user-attributes Name=email,Value=rider@example.com Name=email_verified,Value=true \
      --desired-delivery-mediums EMAIL
  ```

They get an email with the address and a temporary password. To remove someone,
delete or disable their user in the same place. Their files stay on the server until
you delete them (see below).

## Using it

It's the editor you know, with a few differences:

- **Choose…** uploads the file from your computer. A dropped connection carries on
  where it stopped: choose the same file again. A file that's already on the server
  isn't sent twice.
- **Open…** lists your videos on the server. **Delete** removes a video with its settings
  and render.
- **Render video** waits its turn if someone else is rendering. When it's done, a
  **Download** link appears. Finished videos stay downloadable for 30 days
  (`keepRendersDays`).
- **Saved layouts** belong to you; other people don't see them.
- The crew chief speaks with Amazon Polly voices (Brian, Amy, Matthew…) instead of the
  Mac's voices.
- The themes use open fonts on the server (Barlow, Barlow Condensed, Arvo), so text looks
  a little different from your Mac.

## Settings

In `infra/cdk.json` (or `-c name=value` on `cdk deploy`):

| Setting | Default | |
|---|---|---|
| `instanceType` | `c7i.2xlarge` | the server; more vCPUs = faster renders |
| `dataGb` | `200` | disk for everyone's videos (it can grow later, not shrink) |
| `idleMinutes` | `30` | sleep after this long unused (`0` = never) |
| `keepRendersDays` | `30` | how long finished videos stay downloadable |
| `maxUploadGb` | `20` | largest file someone can upload |
| `monthlyBudgetUsd` | `60` | the spending alarm (needs `BUDGET_EMAIL`) |
| `githubRepo`, `githubBranch` | this repo, `main` | who can deploy |

## Looking after it

- **Logs:** Systems Manager → Session Manager → start a session on the server
  (no SSH needed), then `sudo journalctl -u racebox -f`.
- **Restart the app:** in that session, `sudo systemctl restart racebox`. Deploys do this
  for you.
- **Wake it by hand:** `aws ec2 start-instances --instance-ids <InstanceId output>`.
- **Disk space:** in a session, `df -h /data`. Everyone's files are in `/data/users/`.
  To grow the disk, raise `dataGb` and deploy, then in a session run `sudo xfs_growfs /data`.
- **Backups:** EC2 → Elastic Block Store → Volumes → the `Data` volume → Create snapshot
  (or set up Data Lifecycle Manager for a schedule).

## How it fits together (and what's safe)

- Only CloudFront can reach the server: its firewall allows only CloudFront's addresses.
  Everything else needs a Cognito sign-in. The sign-in uses the authorization code flow
  with PKCE and then a signed cookie that lasts 14 days.
- Traffic between CloudFront and the server is plain HTTP inside AWS's network. Between
  your browser and CloudFront it's HTTPS.
- People can only open files in their own folder on the server.
- Videos and settings live on their own encrypted volume, apart from the server's
  system disk. Changing `instanceType` stops and restarts the server; it doesn't replace it.
- `cdk destroy RaceboxServer` deletes the server. It keeps the S3 bucket, the image
  registry and the user pool, and leaves a snapshot of the data volume. Delete those by
  hand if you want them gone.

## Trying the web version on your computer

```sh
pip install ".[server]"
RBO_AUTH=off racebox-overlay serve --host 127.0.0.1 --port 8080
```

Then open http://127.0.0.1:8080/. With sign-in off, everything goes in one person's
folder in `./rbo-data`. Never run it like this where other people can reach it.
