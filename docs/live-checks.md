# Department pilot and live checks

Three checks need access that only you have. Each one is a single command once
you have that access, and all of them are read-only against your systems.

## 1. Pilot on your department's documents

1. Copy the department's documents (any mix of PDF, Word, Excel, PowerPoint,
   HTML, Markdown, text) to `~/Documents/pilot-docs`, or sync them with a
   connector (section 2). Nothing is committed: `data/` is gitignored.
2. Collect about 30 questions from the department's users with
   [samples/pilot-template/questions.yaml](../samples/pilot-template/questions.yaml).
   Each user names the files that answer their question, as paths relative to
   the folder. Include a few questions the documents cannot answer.
3. Build, check and measure:

   ```bash
   okf-ingest suggest-config ~/Documents/pilot-docs > ~/Documents/pilot-docs/_okf.yaml   # review it
   okf-ingest --data-dir data/dept build ~/Documents/pilot-docs
   okf-ingest --data-dir data/dept validate
   okf-ingest --data-dir data/dept eval ~/pilot-questions.yaml
   ```

4. Serve it with `OKF_DATA_DIR=data/dept MCP_PORT=5055 make local` and let the
   users ask their questions through their assistant. Record which answers were
   right, which cited the wrong document, and which were answered although the
   documents had no answer.

Set release targets with the business owner from this baseline. For comparison,
the public GitLab Handbook pilot reached Hit@5 0.93 and MRR 0.83.

## 2. SharePoint and Google Drive against a live tenant

Keep the secrets in a file only you can read, for example
`~/.config/okf/live.env` (`chmod 600`), and never commit it:

```bash
OKF_SHAREPOINT_SECRET=...            # Entra app client secret
OKF_GDRIVE_KEY_FILE=/path/to/key.json
```

- **SharePoint:** register an Entra ID app with the `Sites.Read.All` or
  `Sites.Selected` *application* permission (admin consent) and a client secret.
  Use a test site with a few files that have different permissions: a private
  file, a file shared with a group, and an organization-wide link.
- **Google Drive:** create a service account with the Drive API enabled,
  download its JSON key, and share a test folder with the service account's
  email address (Viewer). Share some files with a group and some with your domain.

Put both in a `connectors.yaml` (see the README, *Connectors*), then run:

```bash
set -a; . ~/.config/okf/live.env; set +a
.venv/bin/python tests/e2e/connectors_live.py connectors.yaml \
    --expect "Policies/leave.docx=group:hr" --expect "Shared/handbook.pdf=*"
```

The check syncs into a temporary mirror (your configured mirror is not touched)
and verifies that:

- the source lists and downloads without errors;
- every file has permissions;
- a second sync downloads nothing;
- the mirror builds into a conformant snapshot;
- each `--expect` principal can read its file.

It also prints who can read what, so you can compare it with the site's or
folder's sharing dialog.

## 3. OpenShift on a live cluster

Use a company cluster or the free
[Red Hat Developer Sandbox](https://developers.redhat.com/developer-sandbox).
Log in yourself (`oc login --token=... --server=...`), run `make ingest-install`
once (the check writes a Word document with python-docx), then run from the
project's virtualenv:

```bash
.venv/bin/python tests/e2e/k8s_e2e.py --platform openshift --context "$(oc config current-context)" --build --namespace "$(oc project -q)"
```

The check builds the server and ingest images in the cluster with the two
BuildConfigs, from the tracked files (so commit or stage new files first). It
then deploys Keycloak and PostgreSQL (test only) and the `deployment/openshift`
overlay, and verifies:

- pods run under restricted-v2 with a UID from the project's range;
- the root filesystem is read-only;
- a refresh Job from the CronJob converts a Word document with Docling;
- the Route serves `/health` and requires a token on `/mcp`;
- per-group visibility;
- a refresh is served without a restart;
- tool calls and ingestion are audited.

With `--namespace`, only the objects the test created are deleted afterwards;
without it, the test creates a new project and deletes it at the end. Add `--keep`
to inspect the result.

To deploy for real: `make deploy openshift NAMESPACE=<project>`.
