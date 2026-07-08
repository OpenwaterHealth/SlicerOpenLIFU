# SlicerOpenLIFU Release Process

This document describes how to release the SlicerOpenLIFU extension. The
extension is released more regularly than the desktop application.

## Versioning

Use git tags of the form:

```text
vX.Y.Z
vX.Y.Z-rc.N
```

Use a new minor version, such as `v1.21.0`, for a planned feature release. Use
a patch version, such as `v1.21.1`, for fixes to an existing release branch.

Special compatibility tags with a suffix, such as `vX.Y.Z+suffix`, should be
rare and must be explained in the release notes.

## Branch Model

`main` is the development branch. Release branches are stabilization branches:

```text
main
release/1.21
  v1.21.0-rc.1
  v1.21.0-rc.2
  v1.21.0
  v1.21.1
```

After a release branch is cut, new feature work continues on `main`. The release
branch is primarily for stabilization fixes.

Fixes should usually be merged to `main` first and cherry-picked to the release
branch. Release-only changes are acceptable for packaging, release notes, or
documentation that would not make sense on `main`.

## Compatibility Inputs

Before cutting a release branch, verify these versioned inputs:

- The required `openlifu` package is pinned in
  `OpenLIFULib/OpenLIFULib/Resources/python-requirements.txt`.
- If the `openlifu` pin changed, the sample database tags in
  `OpenLIFULib/OpenLIFULib/sample_data.py` have been checked and updated if
  needed.
- If a new Python dependency was added, `python_requirements_exist()` in
  `OpenLIFULib/OpenLIFULib/dependency_utils.py` checks for it.
- The target 3D Slicer version is known and will be recorded in the GitHub
  release notes.

The desktop application openlifu-app consumes SlicerOpenLIFU by pinning a
`GIT_TAG` in its top-level `CMakeLists.txt`. Once a SlicerOpenLIFU release is final, update the app
compatibility matrix in the OpenLIFU-app repository if that release is a
candidate for an app bundle.

## Release steps

See the below section on release candidates if pre-releases are needed (typically not needed for SlicerOpenLIFU).

1. Confirm that `main` is in the intended release state.
2. Ensure tests are passing
3. Create the release branch:

   ```bash
   git checkout main
   git pull
   git checkout -b release/X.Y
   git push -u origin release/X.Y
   ```
4. Tag the release from the release branch:

   ```bash
   git tag vX.Y.0
   git push origin vX.Y.0
   ```

5. Create the GitHub release from `vX.Y.0`.

## Patch Releases

Patch releases come from the existing release branch:

```text
release/X.Y
  vX.Y.1
```

Use patch releases for fixes that should be available to existing extension
users or to an OpenLIFU-app release branch without taking unrelated changes from
`main`.

For ordinary bug fixes, merge the fix to `main` first and then cherry-pick it
to the release branch:

```bash
git checkout release/X.Y
git pull
git cherry-pick <fix-commit-sha>
git push origin release/X.Y
```

If the change only applies to the released line, such as a compatibility fix for
a pinned OpenLIFU-app release, it may be made directly on `release/X.Y`.

After the release branch has the intended fixes, validate it and tag the patch
release from that branch:

```bash
git checkout release/X.Y
git pull
git tag vX.Y.1
git push origin vX.Y.1
```

Create the GitHub release from `vX.Y.1`.

## Release Candidate Flow

Release candidates are not typically needed for SlicerOpenLIFU, but in case it makes sense for a particular minor release it would proceed as follows.

1. Confirm that `main` is in the intended release state.
2. Create the release branch:

   ```bash
   git checkout main
   git pull
   git checkout -b release/X.Y
   git push -u origin release/X.Y
   ```

3. Tag the first release candidate from the release branch:

   ```bash
   git tag vX.Y.0-rc.1
   git push origin vX.Y.0-rc.1
   ```

4. Draft a GitHub prerelease for the RC.

Later tag additional RCs as needed:

   ```bash
   git tag vX.Y.0-rc.2
   git push origin vX.Y.0-rc.2
   ```

Once a release candidate is accepted, tag the final release vX.Y.0 from the same release branch.
