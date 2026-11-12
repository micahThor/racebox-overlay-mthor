#!/usr/bin/env bash
# Deploys the club web app from your computer, signed in to AWS as yourself.
# No AWS keys are kept in GitHub: GitHub only builds and publishes the app's image.
#
#   ./deploy.sh                 update the AWS setup, then restart the app on the newest image
#   ./deploy.sh restart         just restart the app (after publishing a new image on GitHub)
#   ./deploy.sh add-user EMAIL  let someone sign in (they get an email with a temporary password)
#   ./deploy.sh url             print the editor's address
#
# Your settings go in infra/local.env (not committed; copy local.env.example).
set -euo pipefail
cd "$(dirname "$0")"
[ -f local.env ] && set -a && . ./local.env && set +a
: "${AWS_REGION:?set AWS_REGION in infra/local.env (the home region of your account)}"
export AWS_REGION CDK_DEFAULT_REGION="$AWS_REGION"
[ -d .venv ] && . .venv/bin/activate

output() {
  aws cloudformation describe-stacks --stack-name RaceboxServer \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

restart() {
  local iid state cid
  iid=$(output InstanceId)
  state=$(aws ec2 describe-instances --instance-ids "$iid" --query 'Reservations[0].Instances[0].State.Name' --output text)
  if [ "$state" != "running" ]; then
    echo "The server is $state (asleep): it uses the newest image when it next wakes up."
    return
  fi
  echo "Restarting the app on the newest image…"
  cid=$(aws ssm send-command --instance-ids "$iid" --document-name AWS-RunShellScript \
        --comment "racebox deploy" --parameters 'commands=["systemctl restart racebox"]' \
        --query Command.CommandId --output text)
  aws ssm wait command-executed --command-id "$cid" --instance-id "$iid" \
    || aws ssm get-command-invocation --command-id "$cid" --instance-id "$iid"
  echo "Done."
}

case "${1:-deploy}" in
  deploy)
    args=()
    [ -n "${BUDGET_EMAIL:-}" ] && args+=(-c "budgetEmail=$BUDGET_EMAIL")
    [ -n "${INSTANCE_TYPE:-}" ] && args+=(-c "instanceType=$INSTANCE_TYPE")
    cdk deploy RaceboxServer --require-approval never ${args[@]+"${args[@]}"}
    restart
    echo "Editor: $(output EditorUrl)"
    ;;
  restart) restart ;;
  add-user)
    email="${2:?usage: ./deploy.sh add-user someone@example.com}"
    aws cognito-idp admin-create-user --user-pool-id "$(output UserPoolId)" --username "$email" \
      --user-attributes Name=email,Value="$email" Name=email_verified,Value=true \
      --desired-delivery-mediums EMAIL --query 'User.UserStatus' --output text
    echo "Invited $email: they'll get an email with the address and a temporary password."
    ;;
  url) output EditorUrl ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
