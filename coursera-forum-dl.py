#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import datetime
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import codecs
import json

from distutils.version import LooseVersion as V


# Test versions of some critical modules.
# We may, perhaps, want to move these elsewhere.
import bs4
import six
from six import iteritems
import requests

from coursera.cookies import (
    AuthenticationFailed, ClassNotFound,
    get_cookies_for_class, make_cookie_values, login, TLSAdapter)
from coursera.credentials import get_credentials, CredentialsError, keyring
from coursera.define import (CLASS_URL, ABOUT_URL, PATH_CACHE,
                     OPENCOURSE_CONTENT_URL, IN_MEMORY_MARKER,
                     FORMAT_MAX_LENGTH, TITLE_MAX_LENGTH)
from coursera.downloaders import get_downloader
from coursera.utils import (clean_filename, get_anchor_format, mkdir_p, fix_url,
                    print_ssl_error_message, normalize_path,
                    decode_input, BeautifulSoup, is_debug_run)

from coursera.network import get_page, get_page_and_url
from coursera.api import CourseraOnDemand, OnDemandCourseMaterialItems
from coursera.filter import skip_format_url

from coursera import __version__

def parse_args(args=None):
    """
    Parse the arguments/options passed to the program on the command line.
    """

    parser = argparse.ArgumentParser(
        description='Download Coursera.org lecture material and resources.')

    # Basic options
    group_basic = parser.add_argument_group('Basic options')

    group_basic.add_argument('class_names',
                             action='store',
                             nargs='+',
                             help='name(s) of the class(es) (e.g. "ml-005")')

    group_basic.add_argument('-u',
                             '--username',
                             dest='username',
                             action='store',
                             default=None,
                             help='coursera username')

    group_basic.add_argument('-p',
                             '--password',
                             dest='password',
                             action='store',
                             default=None,
                             help='coursera password')

    group_basic.add_argument('--path',
                             dest='path',
                             action='store',
                             default='',
                             help='path to where to save the file. (Default: current directory)')
    
    # Advanced authentication
    group_adv_auth = parser.add_argument_group('Advanced authentication options')

    group_adv_auth.add_argument('-c',
                                '--cookies_file',
                                dest='cookies_file',
                                action='store',
                                default=None,
                                help='full path to the cookies.txt file')

    group_adv_auth.add_argument('-n',
                                '--netrc',
                                dest='netrc',
                                nargs='?',
                                action='store',
                                const=True,
                                default=False,
                                help='use netrc for reading passwords, uses default'
                                ' location if no path specified')

    group_adv_auth.add_argument('-k',
                                '--keyring',
                                dest='use_keyring',
                                action='store_true',
                                default=False,
                                help='use keyring provided by operating system to '
                                'save and load credentials')

    group_adv_auth.add_argument('--clear-cache',
                                dest='clear_cache',
                                action='store_true',
                                default=False,
                                help='clear cached cookies')

    # Debug options
    group_debug = parser.add_argument_group('Debugging options')

    group_debug.add_argument('--skip-download',
                             dest='skip_download',
                             action='store_true',
                             default=False,
                             help='for debugging: skip actual downloading of files')

    group_debug.add_argument('--debug',
                             dest='debug',
                             action='store_true',
                             default=False,
                             help='print lots of debug information')

    group_debug.add_argument('--version',
                             dest='version',
                             action='store_true',
                             default=False,
                             help='display version and exit')

    group_debug.add_argument('-l',  # FIXME: remove short option from rarely used ones
                             '--process_local_page',
                             dest='local_page',
                             help='uses or creates local cached version of syllabus'
                             ' page')

    # Final parsing of the options
    args = parser.parse_args(args)

    # Initialize the logging system first so that other functions
    # can use it right away
    if args.debug:
        logging.basicConfig(level=logging.DEBUG,
                            format='%(name)s[%(funcName)s] %(message)s')
    else:
        logging.basicConfig(level=logging.INFO,
                            format='%(message)s')

    # show version?
    if args.version:
        # we use print (not logging) function because version may be used
        # by some external script while logging may output excessive information
        print(__version__)
        sys.exit(0)

    # decode path so we can work properly with cyrillic symbols on different
    # versions on Python
    args.path = decode_input(args.path)

    # check arguments
    if args.use_keyring and args.password:
        logging.warning('--keyring and --password cannot be specified together')
        args.use_keyring = False

    if args.use_keyring and not keyring:
        logging.warning('The python module `keyring` not found.')
        args.use_keyring = False

    if args.cookies_file and not os.path.exists(args.cookies_file):
        logging.error('Cookies file not found: %s', args.cookies_file)
        sys.exit(1)

    if not args.cookies_file:
        try:
            args.username, args.password = get_credentials(
                username=args.username, password=args.password,
                netrc=args.netrc, use_keyring=args.use_keyring)
        except CredentialsError as e:
            logging.error(e)
            sys.exit(1)

    return args


#https://class.coursera.org/posasoftware-001/api/forum/forums/0/threads?page_size=10

def get_api_threads_url(class_name):
    """
    Return the Coursera index/syllabus URL.

    The returned result depends on if we want to only use a preview page or
    if we are enrolled in the course.
    """
    class_type = 'index'
    url = CLASS_URL.format(class_name=class_name) + '/api/forum/forums/0/threads?page_size=10'
    logging.debug('Using %s mode with page: %s', class_type, url)

    return url

def get_api_post_url(class_name, index):
    """
    Return the Coursera index/syllabus URL.

    The returned result depends on if we want to only use a preview page or
    if we are enrolled in the course.
    """
    class_type = 'index'
    url = CLASS_URL.format(class_name=class_name) + '/api/forum/threads/{0}?sort=null'.format(index)
    logging.debug('Using %s mode with page: %s', class_type, url)

    return url


def get_session():
    """
    Create a session with TLS v1.2 certificate.
    """

    session = requests.Session()
    session.mount('https://', TLSAdapter())

    return session

def download_forums(args, class_name):
    """
    Download all forums
    """
    session = get_session()

    
    get_cookies_for_class(session,
                            class_name,
                            cookies_file=args.cookies_file,
                            username=args.username, password=args.password)
    session.cookie_values = make_cookie_values(session.cookies, class_name)
    
    forums_dir = os.path.join(
        args.path, class_name,
        'forums')
    
    if not os.path.exists(forums_dir):
        mkdir_p(normalize_path(forums_dir))
    
    threads = json.loads(get_page(session, get_api_threads_url(class_name)))
    
    threads_num=0    
    if "total_threads" in threads:
        threads_num=int(threads["total_threads"])
        
    for i in range(1,threads_num+1):
        try:
            post = get_page(session, get_api_post_url(class_name, i))
        except requests.exceptions.HTTPError:
            continue
        
        with open(forums_dir + '/{0}.json'.format(i), 'w') as f:
            f.write(post)    

    return 0


def main():
    """
    Main entry point for execution as a program (instead of as a module).
    """

    args = parse_args()
    logging.info('coursera_dl version %s', __version__)
    completed_classes = []

    mkdir_p(PATH_CACHE, 0o700)
    if args.clear_cache:
        shutil.rmtree(PATH_CACHE)

    for class_name in args.class_names:
        try:
            logging.info('Downloading class: %s', class_name)
            if download_forums(args, class_name):
                completed_classes.append(class_name)
        except requests.exceptions.HTTPError as e:
            logging.error('HTTPError %s', e)
        except requests.exceptions.SSLError as e:
            logging.error('SSLError %s', e)
            print_ssl_error_message(e)
            if is_debug_run():
                raise
        except ClassNotFound as cnf:
            logging.error('Could not find class: %s', cnf)
        except AuthenticationFailed as af:
            logging.error('Could not authenticate: %s', af)

    if completed_classes:
        logging.info(
            "Classes which appear completed: " + " ".join(completed_classes))


if __name__ == '__main__':
    main()
    
