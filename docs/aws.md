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

Everything AWS is in `infra/` (CDK, Python). GitHub Actions checks every push and,
when you ask, publishes the app's image to GitHub's image registry. GitHub never signs
in to AWS. You deploy from your computer, signed in as yourself, with `infra/deploy.sh`.

## What it costs (US regions, roughly)

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
signed in as an administrator (`aws sts get-caller-identity` works), Node.js 22 or 24,
and Python 3.9+.

1. **Get the CDK tools** (from the repository's folder):

   ```sh
   cd infra
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   npm install -g aws-cdk
   ```

2. **Your settings.** Copy `infra/local.env.example` to `infra/local.env` and set
   `AWS_REGION`. `local.env` isn't committed, so your email and profile stay on your computer.

   New AWS accounts may only work in one home region. If you get "explicit deny in a
   service control policy" errors, find yours at settings.aws.com → Projects → your
   project → Additional info, and use that region.

3. **Prepare the account for CDK** (once per account and region):

   ```sh
   export AWS_REGION=us-east-2      # the same region as in local.env
   cdk bootstrap
   ```

4. **Publish the app's image.** On GitHub, go to Actions → **Test and publish** →
   **Run workflow** on `main`. Once it's done, make the image public so the server can
   download it: on the repository's main page, open **Packages** → `racebox-overlay-mthor`
   → **Package settings** → **Change visibility** → **Public**. You only do this once.
   The image is the open-source app; it holds no settings or data.

5. **Deploy:**

   ```sh
   ./deploy.sh
   ```

   The first deploy takes about 15 minutes (CloudFront is slow to create). It ends by
   printing the editor's address. `./deploy.sh url` prints it again later.

6. **Let yourself in:**

   ```sh
   ./deploy.sh add-user you@example.com
   ```

   Cognito emails you a temporary password. Open the editor's address, sign in, and
   choose your own password.

## Deploying changes

Pushing code never deploys anything. Each push runs the checks (tests, the Docker image,
the AWS setup). That's free for a public repository, so push as often as you like.
When you want the club's server to have your changes:

1. **Publish:** Actions → **Test and publish** → **Run workflow** (or
   `gh workflow run deploy.yml --ref main`). It publishes only if the checks pass.
2. **Restart the app:** from `infra/`, run `./deploy.sh restart`. If you changed
   anything in `infra/`, run `./deploy.sh` instead, which updates the AWS setup too.
   If the server is asleep, there's nothing to do: it picks up the newest image when it wakes.

## Adding club members

Nobody can sign up on their own. To add someone:

```sh
./deploy.sh add-user rider@example.com
```

Or in the console: Cognito → User pools → `racebox-overlay` → Users → Create user.
Choose "Send an email invitation", enter their email address, and tick "Mark email address
as verified".

They get an email with the address and a temporary password. To remove someone,
delete or disable their user in the console. Their files stay on the server until
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

In `infra/cdk.json`. `INSTANCE_TYPE` and `BUDGET_EMAIL` can also go in `infra/local.env`.

| Setting | Default | |
|---|---|---|
| `instanceType` | `c7i.2xlarge` | the server; more vCPUs = faster renders |
| `dataGb` | `200` | disk for everyone's videos (it can grow later, not shrink) |
| `idleMinutes` | `30` | sleep after this long unused (`0` = never) |
| `keepRendersDays` | `30` | how long finished videos stay downloadable |
| `maxUploadGb` | `20` | largest file someone can upload |
| `monthlyBudgetUsd` | `60` | the spending alarm (needs `BUDGET_EMAIL` in `local.env`) |
| `image` | this repo's image on ghcr.io | the app image the server runs |

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
- `cdk destroy RaceboxServer` deletes the server. It keeps the S3 bucket and the user
  pool, and leaves a snapshot of the data volume. Delete those by
  hand if you want them gone.

## Trying the web version on your computer

You don't need this to deploy. It runs the club version on your Mac, so you can try it
without AWS. From the repository's folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install ".[server]"
RBO_AUTH=off racebox-overlay serve --host 127.0.0.1 --port 8080
```

Then open http://127.0.0.1:8080/ in your browser. With sign-in off, everything goes in
one person's folder in `./rbo-data`, apart from your normal `racebox-overlay gui`
projects. Never run it like this where other people can reach it.

The `.venv` only has to be made once. Next time, run `source .venv/bin/activate` and
then the `racebox-overlay serve` line. On a Mac, use the venv's `pip`; without a venv the
command is `pip3`, because plain `pip` doesn't exist.
