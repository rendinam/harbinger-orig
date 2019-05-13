#!/usr/bin/env python
#
# Harbinger runs on an internet-visible host and communicates with Github.
# Meant to be run on a schedule, it polls a collection of software projects
# for updates since the last logged version and posts an issue to the target
# project's github page indicating which dependency has a new version
# available.
# The polling logic to use for each project is defined by one or more plugin
# modules.

import os
import sys
import configparser
import urllib.request
import subprocess
from subprocess import run
import tarfile
import tempfile
import argparse
import shutil
from contextlib import contextmanager
import importlib

import yaml
import getpass
from abc import ABC, abstractmethod
import github3


@contextmanager
def pushd(newDir):
    '''Context manager function for shell-like pushd functionality

    Allows for constructs like:
    with pushd(directory):
        'code'...
    When 'code' is finished, the working directory is restored to what it
    was when pushd was invoked.'''
    previousDir = os.getcwd()
    os.chdir(newDir)
    yield
    os.chdir(previousDir)


class Plugin(ABC):
    def __init__(self):
        super().__init__()

    @abstractmethod
    def get_version(self):
        pass

    @abstractmethod
    def get_changelog(self):
        pass
    


class ReleaseNotifier():
    '''ReleaseNotifier class

    Parameters
    ----------
    depname: Dependency name (must map to a plugin name to support
                                    querying the version information.)
    params: Notification configuration for a single dependency.
                  (dict)
    refdir: The directory holding the reference version value for the
            dependency to be queried.
    '''
    def __init__(self, depname, params, refdir, gh_username, gh_password):
        # Normalize path-like dependency names.
        self.dep_name = depname
        self.depchecker = None
        self.params = params
        self.github = None
        self.plugin = None
        self.plugin_extra = None  # Optional extra data to pass to a plugin.
        self.gh_username = gh_username
        self.gh_password = gh_password
        self.refdir = refdir
        self.ref_file = self.gen_ref_filename()
        self.new_ver_data = None
        self.md5 = None
        self.ref_md5 = None
        self.issue_title_base = 'Upstream release of dependency: '
        self.issue_title = self.issue_title_base + self.dep_name
        self.comment_base = ('This is a message from an automated system '
                             'that monitors `{}` '
                             'releases.\n'.format(self.dep_name))
        self.dry_run = False
        self.remote_ver = None

    def gen_ref_filename(self):
        '''Generate reference filename
        
        Returns
        -------
        Dependency-specific version reference identifier.
        '''
        norm_depname = self.dep_name.replace('/', '-')
        return os.path.join(self.refdir, '{}_reference'.format(norm_depname))

    def load_plugin(self):
        if len(self.params.keys()) == 0:
            plugin_name = f'.plugins.relcheck_{depname}'
        else:
            plugin_name = '.plugins.' + self.params['plugin'].strip()
        print(f'plugin_name = {plugin_name}')
        try:
            self.depchecker = importlib.import_module(plugin_name, 'harbinger')
        except Exception as e:
            print(f'Import of plugin {plugin_name} failed.\n\n')
            raise(ImportError)
        self.plugin = self.depchecker.plugin
        if 'github' in plugin_name:
            print('Authenticating with github API...')
            self.github = github3.login(self.gh_username, self.gh_password)
            self.plugin_extra = self.github

    def get_version(self):
        '''Call the version retrieval method of a plugin.
        
        This is the version retrieval entry point for the plugin assigned
        as the release checker for this notifier instance.
        
        Returns
        -------
        Return value of the get_version method of the plugin associated with
        the dependency in question which is a dict containing at least a
        'version' key.
        '''
        #return self.depchecker.get_version(self.dep_name, self.params, self.plugin_extra)
        return self.plugin.get_version(self.dep_name, self.params, self.plugin_extra)

    def get_changelog(self, ref_ver_data, new_ver_data):
        '''Call the changelog retrieval method of a plugin.
        
        This is the changelog retrieval entry point for the plugin assigned
        as the release checker for this notifier instance.

        Returns
        -------
        Return value of the get_changelog method of the plugin associated with
        the dependency in question which is a string.
        '''
        #return self.depchecker.get_changelog(ref_ver_data, new_ver_data, self.plugin_extra)
        return self.plugin.get_changelog(ref_ver_data, new_ver_data, self.plugin_extra)

    def new_version(self):
        '''Determine if the version of this dependency is newer than the value
        stored in 

        Test whether or not the version data for the dependency in question is
        newer than the reference value stored.

        Returns
        -------
        True if version info retrieved is newer than the reference value.
        False otherwise.
        '''
        print('Reading version reference info from: {}'.format(self.ref_file))
        with open(self.ref_file) as f:
            ver_info = yaml.safe_load(f)
        if self.remote_ver != ver_info['version']:
            return True
        else:
            return False

    def reference_available(self):
        '''Is version reference data available for the dependency?
        
        Returns
        -------
        True if reference file is available.
        False otherwise.
        '''
        if os.path.isfile(self.ref_file):
            return True
        else:
            return False

    def read_reference(self):
        '''Read YAML dependency version reference data

        Store the YAML in the self.ref_ver_data dict.
        '''
        with open(self.ref_file) as f:
            self.ref_ver_data = yaml.safe_load(f)

    def create_github_issue(self):
        # Push changes text to a new/existing issue on Github.
        self.comment = self.comment_base + '/n' + self.comment
        if self.dry_run:
            print(self.comment)
        else:
            print('Posting comment to Github...')
            if not self.github:
                gh = github3.login(args.username, password=password)
            repo = args.notify_repo.split('/')
            self.github.create_issue(repo[0], repo[1], self.issue_title, self.comment)

    def write_version_ref(self):
        with open(self.ref_file, 'w') as f:
            reference = yaml.safe_dump(self.new_ver_data)
            print(reference)
            f.write(reference)

    def update_version_ref(self):
        # Update version reference to inform the next run.
        os.chdir(sys.path[0])
        print('Backing up old version reference...')
        self.ref_backup = self.ref_file + '.bkup'
        shutil.copy(self.ref_file, self.ref_backup)

        print('Updating {} version refrence.  '.format(self.dep_name), end='')
        try:
            self.write_version_ref()
            print('Done.')
        except e:
            print('\nERROR writing reference file. To provide the correct '
                  'reference\nfor the next run and avoid duplicated Github '
                  'issue comments, \ncopy the following line as the sole '
                  'contents the file \n{}.\n\n'
                  '{}'.format(self.ref_file, reference))

    def post_notification(self):
        self.update_version_ref()
        # For each notification type desired, post one.
        # TODO: support other notification types?
        try:
            self.create_github_issue()
        except e:
            # roll back version reference
            shutilcopy(self.ref_backup, self.ref_file)

    def check_for_release(self):
        self.load_plugin()
        with tempfile.TemporaryDirectory() as self.tmpdir:
            with pushd(self.tmpdir):
                self.new_ver_data = self.get_version()
                self.remote_ver = self.new_ver_data['version']
            # Check for presence of version reference. If it does not exist,
            # bootstrap future queries by storing the current remote version
            # info as the reference.
            if self.reference_available():
                self.read_reference()
                if self.new_version():
                    with pushd(self.tmpdir):
                        self.comment = self.get_changelog(
                                self.ref_ver_data, self.new_ver_data)
                        self.post_notification()
                else:
                    print('No new version detected for {}.'.format(
                        self.dep_name))
            else:
                print('No existing version reference found for {}'.format(
                    self.dep_name))
                print('Storing remote version as new reference: {}'.format(
                    os.path.join(self.refdir, self.ref_file)))
                self.write_version_ref()
