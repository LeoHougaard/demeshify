# Security policy

## Supported version

Security fixes currently target the latest commit on `main` during the public
beta. Releases will list a support window once the project starts publishing
versioned builds.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Do not open a
public issue with exploit details, private model files, credentials, or local
filesystem paths. Include the affected commit, impact, reproduction steps, and a
small synthetic input if one is needed.

## Deployment boundary

STL to STEP Converter is a local, single-user application. The documented launcher binds
to `127.0.0.1`, and the Codespace port is private. The beta has no authentication,
tenant isolation, storage quota, or automatic artifact deletion. Do not expose
it as a public multi-user service without adding those controls and a durable,
bounded job system.

Uploaded STLs, reports, generated scripts, and CAD exports remain in the runs
directory until the operator deletes them.
