# TEMPORARY P5 VALIDATION ONLY

This directory exists only to trigger `deploy-staging.yml` from the
`feat/p5-devsecops-cicd` branch before P5 is merged to `main`
(`workflow_dispatch` only registers from the default branch).

`image_sha` holds the 40-char Git SHA of the ACR images to deploy. Pushing a
change to this file on the P5 branch starts the deployment **plan** job; the
`staging` environment required-reviewer gate still blocks the apply.

Remove this directory and the `push:` trigger block in `deploy-staging.yml` at
P5 close, leaving only `workflow_dispatch` on `main`.
