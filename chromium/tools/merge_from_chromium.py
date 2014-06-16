#!/usr/bin/python
#
# Copyright (C) 2012 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Merge Chromium into the Android tree."""

import contextlib
import logging
import optparse
import os
import re
import sys
import urllib2

import merge_common


# We need to import this *after* merging from upstream to get the latest
# version. Set it to none here to catch uses before it's imported.
webview_licenses = None


AUTOGEN_MESSAGE = 'This commit was generated by merge_from_chromium.py.'
SRC_GIT_BRANCH = 'refs/remotes/history/upstream-master'


def _ReadGitFile(sha1, path, git_url=None, git_branch=None):
  """Reads a file from a (possibly remote) git project at a specific revision.

  Args:
    sha1: The SHA1 at which to read.
    path: The relative path of the file to read.
    git_url: The URL of the git server, if reading a remote project.
    git_branch: The branch to fetch, if reading a remote project.
  Returns:
    The contents of the specified file.
  """
  if git_url:
    merge_common.GetCommandStdout(['git', 'fetch', '-f', git_url, git_branch])
  return merge_common.GetCommandStdout(['git', 'show', '%s:%s' % (sha1, path)])


def _ParseDEPS(deps_content):
  """Parses the .DEPS.git file from Chromium and returns its contents.

  Args:
    deps_content: The contents of the .DEPS.git file as text.
  Returns:
    A dictionary of the contents of .DEPS.git at the specified revision
  """

  class FromImpl(object):
    """Used to implement the From syntax."""

    def __init__(self, module_name):
      self.module_name = module_name

    def __str__(self):
      return 'From("%s")' % self.module_name

  class _VarImpl(object):
    def __init__(self, custom_vars, local_scope):
      self._custom_vars = custom_vars
      self._local_scope = local_scope

    def Lookup(self, var_name):
      """Implements the Var syntax."""
      if var_name in self._custom_vars:
        return self._custom_vars[var_name]
      elif var_name in self._local_scope.get('vars', {}):
        return self._local_scope['vars'][var_name]
      raise Exception('Var is not defined: %s' % var_name)

  tmp_locals = {}
  var = _VarImpl({}, tmp_locals)
  tmp_globals = {'From': FromImpl, 'Var': var.Lookup, 'deps_os': {}}
  exec(deps_content) in tmp_globals, tmp_locals
  return tmp_locals


def _GetProjectMergeInfo(projects, deps_vars):
  """Gets the git URL and SHA1 for each project based on .DEPS.git.

  Args:
    projects: The list of projects to consider.
    deps_vars: The dictionary of dependencies from .DEPS.git.
  Returns:
    A dictionary from project to git URL and SHA1 - 'path: (url, sha1)'
  Raises:
    TemporaryMergeError: if a project to be merged is not found in .DEPS.git.
  """
  deps_fallback_order = [
      deps_vars['deps'],
      deps_vars['deps_os']['unix'],
      deps_vars['deps_os']['android'],
  ]
  result = {}
  for path in projects:
    for deps in deps_fallback_order:
      if len(path) > 0:
        upstream_path = os.path.join('src', path)
      else:
        upstream_path = 'src'
      url_plus_sha1 = deps.get(upstream_path)
      if url_plus_sha1:
        break
    else:
      raise merge_common.TemporaryMergeError(
          'Could not find .DEPS.git entry for project %s. This probably '
          'means that the project list in merge_from_chromium.py needs to be '
          'updated.' % path)
    match = re.match('(.*?)@(.*)', url_plus_sha1)
    url = match.group(1)
    sha1 = match.group(2)
    logging.debug('  Got URL %s and SHA1 %s for project %s', url, sha1, path)
    result[path] = {'url': url, 'sha1': sha1}
  return result


def _MergeProjects(version, root_sha1, target, unattended, buildspec_url):
  """Merges each required Chromium project into the Android repository.

  .DEPS.git is consulted to determine which revision each project must be merged
  at. Only a whitelist of required projects are merged.

  Args:
    version: The version to mention in generated commit messages.
    root_sha1: The git hash to merge in the root repository.
    target: The target branch to merge to.
    unattended: Run in unattended mode.
    buildspec_url: URL for buildspec repository, when merging a branch.
  Raises:
    TemporaryMergeError: If incompatibly licensed code is left after pruning.
  """
  # The logic for this step lives here, in the Android tree, as it makes no
  # sense for a Chromium tree to know about this merge.

  if unattended:
    branch_create_flag = '-B'
  else:
    branch_create_flag = '-b'
  branch_name = 'merge-from-chromium-%s' % version

  logging.debug('Parsing DEPS ...')
  if root_sha1:
    deps_content = _ReadGitFile(root_sha1, '.DEPS.git')
  else:
    deps_content = _ReadGitFile('FETCH_HEAD', version + '/DEPS',
                                buildspec_url, 'master')

  deps_vars = _ParseDEPS(deps_content)

  merge_info = _GetProjectMergeInfo(merge_common.THIRD_PARTY_PROJECTS,
                                    deps_vars)

  for path in merge_info:
    # webkit needs special handling as we have a local mirror
    local_mirrored = path == 'third_party/WebKit'
    url = merge_info[path]['url']
    sha1 = merge_info[path]['sha1']
    dest_dir = os.path.join(merge_common.REPOSITORY_ROOT, path)
    if local_mirrored:
      remote = 'history'
    else:
      remote = 'goog'
    merge_common.GetCommandStdout(['git', 'checkout',
                                   branch_create_flag, branch_name,
                                   '-t', remote + '/' + target],
                                  cwd=dest_dir)
    if not local_mirrored or not root_sha1:
      logging.debug('Fetching project %s at %s ...', path, sha1)
      fetch_args = ['git', 'fetch', url, sha1]
      merge_common.GetCommandStdout(fetch_args, cwd=dest_dir)
    if merge_common.GetCommandStdout(['git', 'rev-list', '-1', 'HEAD..' + sha1],
                                     cwd=dest_dir):
      logging.debug('Merging project %s at %s ...', path, sha1)
      # Merge conflicts make git merge return 1, so ignore errors
      merge_common.GetCommandStdout(['git', 'merge', '--no-commit', sha1],
                                    cwd=dest_dir, ignore_errors=True)
      merge_common.CheckNoConflictsAndCommitMerge(
          'Merge %s from %s at %s\n\n%s' % (path, url, sha1, AUTOGEN_MESSAGE),
          cwd=dest_dir, unattended=unattended)
    else:
      logging.debug('No new commits to merge in project %s', path)

  # Handle root repository separately.
  merge_common.GetCommandStdout(['git', 'checkout',
                                 branch_create_flag, branch_name,
                                 '-t', 'history/' + target])
  if not root_sha1:
    merge_info = _GetProjectMergeInfo([''], deps_vars)
    url = merge_info['']['url']
    root_sha1 = merge_info['']['sha1']
    merge_common.GetCommandStdout(['git', 'fetch', url, root_sha1])
  logging.debug('Merging Chromium at %s ...', root_sha1)
  # Merge conflicts make git merge return 1, so ignore errors
  merge_common.GetCommandStdout(['git', 'merge', '--no-commit', root_sha1],
                                ignore_errors=True)
  merge_common.CheckNoConflictsAndCommitMerge(
      'Merge Chromium at %s (%s)\n\n%s'
      % (version, root_sha1, AUTOGEN_MESSAGE), unattended=unattended)

  logging.debug('Getting directories to exclude ...')

  # We import this now that we have merged the latest version.
  # It imports to a global in order that it can be used to generate NOTICE
  # later. We also disable writing bytecode to keep the source tree clean.
  sys.path.append(os.path.join(merge_common.REPOSITORY_ROOT, 'android_webview',
                               'tools'))
  sys.dont_write_bytecode = True
  global webview_licenses
  import webview_licenses
  import known_issues

  for path, exclude_list in known_issues.KNOWN_INCOMPATIBLE.iteritems():
    logging.debug('  %s', '\n  '.join(os.path.join(path, x) for x in
                                      exclude_list))
    dest_dir = os.path.join(merge_common.REPOSITORY_ROOT, path)
    merge_common.GetCommandStdout(['git', 'rm', '-rf', '--ignore-unmatch'] +
                                  exclude_list, cwd=dest_dir)
    if _ModifiedFilesInIndex(dest_dir):
      merge_common.GetCommandStdout(['git', 'commit', '-m',
                                     'Exclude unwanted directories'],
                                    cwd=dest_dir)


def _CheckLicenses():
  """Check that no incompatibly licensed directories exist."""
  directories_left_over = webview_licenses.GetIncompatibleDirectories()
  if directories_left_over:
    raise merge_common.TemporaryMergeError(
        'Incompatibly licensed directories remain: ' +
        '\n'.join(directories_left_over))


def _GenerateMakefiles(version, unattended):
  """Run gyp to generate the Android build system makefiles.

  Args:
    version: The version to mention in generated commit messages.
    unattended: Run in unattended mode.
  """
  logging.debug('Generating makefiles ...')

  # TODO(torne): come up with a way to deal with hooks from DEPS properly
  # Run libaddressinput hook as we need this to build.
  merge_common.GetCommandStdout(['python',
      'src/third_party/libaddressinput/chromium/tools/update-strings.py'])

  # TODO(torne): The .tmp files are generated by
  # third_party/WebKit/Source/WebCore/WebCore.gyp/WebCore.gyp into the source
  # tree. We should avoid this, or at least use a more specific name to avoid
  # accidentally removing or adding other files.
  for path in merge_common.ALL_PROJECTS:
    dest_dir = os.path.join(merge_common.REPOSITORY_ROOT, path)
    merge_common.GetCommandStdout(['git', 'rm', '--ignore-unmatch',
                                   'GypAndroid.*.mk', '*.target.*.mk',
                                   '*.host.*.mk', '*.tmp'], cwd=dest_dir)

  try:
    merge_common.GetCommandStdout(['android_webview/tools/gyp_webview', 'all'])
  except merge_common.MergeError as e:
    if not unattended:
      raise
    else:
      for path in merge_common.ALL_PROJECTS:
        merge_common.GetCommandStdout(
            ['git', 'reset', '--hard'],
            cwd=os.path.join(merge_common.REPOSITORY_ROOT, path))
      raise merge_common.TemporaryMergeError('Makefile generation failed: ' +
                                             str(e))

  for path in merge_common.ALL_PROJECTS:
    dest_dir = os.path.join(merge_common.REPOSITORY_ROOT, path)
    # git add doesn't have an --ignore-unmatch so we have to do this instead:
    merge_common.GetCommandStdout(['git', 'add', '-f', 'GypAndroid.*.mk'],
                                  ignore_errors=True, cwd=dest_dir)
    merge_common.GetCommandStdout(['git', 'add', '-f', '*.target.*.mk'],
                                  ignore_errors=True, cwd=dest_dir)
    merge_common.GetCommandStdout(['git', 'add', '-f', '*.host.*.mk'],
                                  ignore_errors=True, cwd=dest_dir)
    merge_common.GetCommandStdout(['git', 'add', '-f', '*.tmp'],
                                  ignore_errors=True, cwd=dest_dir)
    # Only try to commit the makefiles if something has actually changed.
    if _ModifiedFilesInIndex(dest_dir):
      merge_common.GetCommandStdout(
          ['git', 'commit', '-m',
           'Update makefiles after merge of Chromium at %s\n\n%s' %
           (version, AUTOGEN_MESSAGE)], cwd=dest_dir)


def _ModifiedFilesInIndex(cwd=merge_common.REPOSITORY_ROOT):
  """Returns true if git's index contains any changes."""
  status = merge_common.GetCommandStdout(['git', 'status', '--porcelain'],
                                         cwd=cwd)
  return re.search(r'^[MADRC]', status, flags=re.MULTILINE) is not None


def _GenerateNoticeFile(version):
  """Generates and commits a NOTICE file containing code licenses.

  This covers all third-party code (from Android's perspective) that lives in
  the Chromium tree.

  Args:
    version: The version to mention in generated commit messages.
  """
  logging.debug('Regenerating NOTICE file ...')

  contents = webview_licenses.GenerateNoticeFile()

  with open(os.path.join(merge_common.REPOSITORY_ROOT, 'NOTICE'), 'w') as f:
    f.write(contents)
  merge_common.GetCommandStdout(['git', 'add', 'NOTICE'])
  # Only try to commit the NOTICE update if the file has actually changed.
  if _ModifiedFilesInIndex():
    merge_common.GetCommandStdout([
        'git', 'commit', '-m',
        'Update NOTICE file after merge of Chromium at %s\n\n%s'
        % (version, AUTOGEN_MESSAGE)])


def _GenerateLastChange(version):
  """Write a build/util/LASTCHANGE file containing the current revision.

  The revision number is compiled into the binary at build time from this file.

  Args:
    version: The version to mention in generated commit messages.
  """
  logging.debug('Updating LASTCHANGE ...')
  svn_revision, sha1 = _GetSVNRevisionAndSHA1('HEAD', 'HEAD')
  with open(os.path.join(merge_common.REPOSITORY_ROOT, 'build/util/LASTCHANGE'),
            'w') as f:
    f.write('LASTCHANGE=%s\n' % svn_revision)
  merge_common.GetCommandStdout(['git', 'add', '-f', 'build/util/LASTCHANGE'])
  logging.debug('Updating LASTCHANGE.blink ...')
  with open(os.path.join(merge_common.REPOSITORY_ROOT,
                         'build/util/LASTCHANGE.blink'), 'w') as f:
    f.write('LASTCHANGE=%s\n' % _GetBlinkRevision())
  merge_common.GetCommandStdout(['git', 'add', '-f',
                                 'build/util/LASTCHANGE.blink'])
  if _ModifiedFilesInIndex():
    merge_common.GetCommandStdout([
        'git', 'commit', '-m',
        'Update LASTCHANGE file after merge of Chromium at %s\n\n%s'
        % (version, AUTOGEN_MESSAGE)])


def GetLKGR():
  """Fetch the last known good release from Chromium's dashboard.

  Returns:
    The last known good SVN revision.
  """
  with contextlib.closing(
      urllib2.urlopen('https://chromium-status.appspot.com/lkgr')) as lkgr:
    return int(lkgr.read())


def GetHEAD():
  """Fetch the latest HEAD revision from the git mirror of the Chromium svn
  repo.

  Returns:
    The latest HEAD SVN revision.
  """
  (svn_revision, root_sha1) = _GetSVNRevisionAndSHA1(SRC_GIT_BRANCH,
                                                     'HEAD')
  return int(svn_revision)


def _ParseSvnRevisionFromGitCommitMessage(commit_message):
  return re.search(r'^git-svn-id: .*@([0-9]+)', commit_message,
                   flags=re.MULTILINE).group(1)


def _GetSVNRevisionFromSha(sha1):
  commit = merge_common.GetCommandStdout([
      'git', 'show', '--format=%H%n%b', sha1])
  return _ParseSvnRevisionFromGitCommitMessage(commit)


def _GetSVNRevisionAndSHA1(git_branch, svn_revision):
  logging.debug('Getting SVN revision and SHA1 ...')

  if svn_revision == 'HEAD':
    # Just use the latest commit.
    commit = merge_common.GetCommandStdout([
        'git', 'log', '-n1', '--grep=git-svn-id:', '--format=%H%n%b',
        git_branch])
    sha1 = commit.split()[0]
    svn_revision = _ParseSvnRevisionFromGitCommitMessage(commit)
    return (svn_revision, sha1)

  if svn_revision is None:
    # Fetch LKGR from upstream.
    svn_revision = GetLKGR()
  output = merge_common.GetCommandStdout([
      'git', 'log', '--grep=git-svn-id: .*@%s' % svn_revision,
      '--format=%H', git_branch])
  if not output:
    raise merge_common.TemporaryMergeError('Revision %s not found in git repo.'
                                           % svn_revision)
  # The log grep will sometimes match reverts/reapplies of commits. We take the
  # oldest (last) match because the first time it appears in history is
  # overwhelmingly likely to be the correct commit.
  sha1 = output.split()[-1]
  return (svn_revision, sha1)


def _GetBlinkRevision():
  commit = merge_common.GetCommandStdout([
      'git', 'log', '-n1', '--grep=git-svn-id:', '--format=%H%n%b'],
      cwd=os.path.join(merge_common.REPOSITORY_ROOT, 'third_party', 'WebKit'))
  return _ParseSvnRevisionFromGitCommitMessage(commit)


def Snapshot(svn_revision, root_sha1, release, target, unattended,
             buildspec_url):
  """Takes a snapshot of the Chromium tree and merges it into Android.

  Android makefiles and a top-level NOTICE file are generated and committed
  after the merge.

  Args:
    svn_revision: The SVN revision in the Chromium repository to merge from.
    root_sha1: The sha1 in the Chromium git mirror to merge from.
    release: The Chromium release version to merge from (e.g. "30.0.1599.20").
             Only one of svn_revision, root_sha1 and release should be
             specified.
    target: The target branch to merge to.
    unattended: Run in unattended mode.
    buildspec_url: URL for buildspec repository, used when merging a release.

  Returns:
    True if new commits were merged; False if no new commits were present.
  """
  if svn_revision:
    svn_revision, root_sha1 = _GetSVNRevisionAndSHA1(SRC_GIT_BRANCH,
                                                     svn_revision)
  elif root_sha1:
    svn_revision = _GetSVNRevisionFromSha(root_sha1)

  if svn_revision and root_sha1:
    version = svn_revision
    if not merge_common.GetCommandStdout(['git', 'rev-list', '-1',
                                          'HEAD..' + root_sha1]):
      logging.info('No new commits to merge at %s (%s)',
                   svn_revision, root_sha1)
      return False
  elif release:
    version = release
    root_sha1 = None
  else:
    raise merge_common.MergeError('No merge source specified')

  logging.info('Snapshotting Chromium at %s (%s)', version, root_sha1)

  # 1. Merge, accounting for excluded directories
  _MergeProjects(version, root_sha1, target, unattended, buildspec_url)

  # 2. Generate Android makefiles
  _GenerateMakefiles(version, unattended)

  # 3. Check for incompatible licenses
  _CheckLicenses()

  # 4. Generate Android NOTICE file
  _GenerateNoticeFile(version)

  # 5. Generate LASTCHANGE file
  _GenerateLastChange(version)

  return True


def Push(version, target):
  """Push the finished snapshot to the Android repository."""
  src = 'merge-from-chromium-%s' % version
  # Use forced pushes ('+' prefix) for the temporary and archive branches in
  # case they already got updated by a previous (possibly failed?) merge, but
  # do not force push to the real master-chromium branch as this could erase
  # downstream changes.
  refspecs = ['%s:%s' % (src, target),
              '+%s:refs/archive/chromium-%s' % (src, version)]
  if target == 'master-chromium':
    refspecs.insert(0, '+%s:master-chromium-merge' % src)
  for refspec in refspecs:
    logging.debug('Pushing to server (%s) ...' % refspec)
    for path in merge_common.ALL_PROJECTS:
      if path in merge_common.PROJECTS_WITH_FLAT_HISTORY:
        remote = 'history'
      else:
        remote = 'goog'
      logging.debug('Pushing %s', path)
      dest_dir = os.path.join(merge_common.REPOSITORY_ROOT, path)
      merge_common.GetCommandStdout(['git', 'push', remote, refspec],
                                    cwd=dest_dir)


def main():
  parser = optparse.OptionParser(usage='%prog [options]')
  parser.epilog = ('Takes a snapshot of the Chromium tree at the specified '
                   'Chromium SVN revision and merges it into this repository. '
                   'Paths marked as excluded for license reasons are removed '
                   'as part of the merge. Also generates Android makefiles and '
                   'generates a top-level NOTICE file suitable for use in the '
                   'Android build.')
  parser.add_option(
      '', '--svn_revision',
      default=None,
      help=('Merge to the specified chromium SVN revision, rather than using '
            'the current LKGR. Can also pass HEAD to merge from tip of tree. '
            'Only one of svn_revision, sha1 and release should be specified'))
  parser.add_option(
      '', '--sha1',
      default=None,
      help=('Merge to the specified chromium sha1 revision from ' + SRC_GIT_BRANCH
            + ' branch, rather than using the current LKGR. Only one of'
            'svn_revision, sha1 and release should be specified.'))
  parser.add_option(
      '', '--release',
      default=None,
      help=('Merge to the specified chromium release buildspec (e.g. '
            '"30.0.1599.20"). Only one of svn_revision, sha1 and release '
            'should be specified.'))
  parser.add_option(
      '', '--buildspec_url',
      default=None,
      help=('Git URL for buildspec repository.'))
  parser.add_option(
      '', '--target',
      default='master-chromium', metavar='BRANCH',
      help=('Target branch to push to. Defaults to master-chromium.'))
  parser.add_option(
      '', '--push',
      default=False, action='store_true',
      help=('Push the result of a previous merge to the server. Note '
            'svn_revision must be given.'))
  parser.add_option(
      '', '--get_lkgr',
      default=False, action='store_true',
      help=('Just print the current LKGR on stdout and exit.'))
  parser.add_option(
      '', '--get_head',
      default=False, action='store_true',
      help=('Just print the current HEAD revision on stdout and exit.'))
  parser.add_option(
      '', '--unattended',
      default=False, action='store_true',
      help=('Run in unattended mode.'))
  parser.add_option(
      '', '--no_changes_exit',
      default=0, type='int',
      help=('Exit code to use if there are no changes to merge, for scripts.'))
  (options, args) = parser.parse_args()
  if args:
    parser.print_help()
    return 1

  if 'ANDROID_BUILD_TOP' not in os.environ:
    print >>sys.stderr, 'You need to run the Android envsetup.sh and lunch.'
    return 1

  logging.basicConfig(format='%(message)s', level=logging.DEBUG,
                      stream=sys.stdout)

  if options.get_lkgr:
    print GetLKGR()
  elif options.get_head:
    logging.disable(logging.CRITICAL)  # Prevent log messages
    print GetHEAD()
  elif options.push:
    if options.release:
      Push(options.release, options.target)
    elif options.svn_revision:
      Push(options.svn_revision, options.target)
    else:
      print >>sys.stderr, 'You need to pass the version to push.'
      return 1
  else:
    if not Snapshot(options.svn_revision, options.sha1, options.release,
                    options.target, options.unattended, options.buildspec_url):
      return options.no_changes_exit

  return 0

if __name__ == '__main__':
  sys.exit(main())
