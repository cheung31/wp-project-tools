# -*- coding: utf-8 -*-
# Chicago Tribune News Applications fabfile
# No copying allowed

import os
import subprocess
import urllib

from time import strftime, localtime

from fabric.api import *
from fabric.contrib.console import confirm
from fabric.context_managers import cd

from getpass import getpass, getuser

from data.fabsettings import *

# This defaults the run and sudo functions to local, so we don't have to duplicate
# code for local development and deployed servers.
env.sudo = local
env.run = local

env.cache_servers = ["lb1", "lb2", "lb3"]

"""
Commands - setup
"""
def git_clone_repo():
    """
    Do initial clone of the git repository.
    """
    with settings(warn_only=True):
        run('git clone %(gitrepo)s %(path)s' % env)

def git_checkout():
    """
    Pull the latest code on the specified branch.
    """
    with cd(env.path):
        if env.gitbranch != 'master':
            with settings(warn_only=True):
                run('git checkout -b %(gitbranch)s origin/%(gitbranch)s' % env)
        run('git checkout %(gitbranch)s' % env)
        run('git pull origin %(gitbranch)s' % env)

        if env.gitsubmodules:
            run('git submodule init')
            run('git submodule update --recursive')

def git_tag_stable():
    """
    Tag branch with datetime release number
    """
    today = strftime('%y.%m.%d', localtime())
    
    print "Checking for tags..."
    
    with settings(hide('warnings'), warn_only=True):
        response = local("git tag -l | grep '^%s'" % today)
        if response.failed:
            tag = '%s-0' % today
            
            print "Found no tags for today. Tagging with %s." % tag
            local('git tag %s' % tag)
            local('git push --tags')
        else:
            numbers = [int(line.split('-')[1]) for line in response.splitlines()]
            numbers.sort()
            
            next = numbers[-1] + 1
            tag = '%s-%s' % (today, next)
            
            print "Found tags for today's date. Incrementing -- tagging with %s." % tag
            local('git tag %s' % tag)
            local('git push --tags')

def install_apache_conf():
    """
    Setup the apache config file
    """
    with cd(env.path):
        sudo('cp apache/%(settings)s-apache.conf ~/apache/%(project_name)s' % env)
        sudo('service apache2 reload' % env)
        run('run-for-cluster -t app "sudo service apache2 reload"')

def install_nginx_conf():
    """
    Setup the nginx config file
    """
    with cd(env.path):
        sudo('cp apache/%(settings)s-nginx.conf ~/nginx/%(project_name)s' % env)
        run('run-for-cluster -t app "sudo service nginx reload"')

"""
Commands - deployment
"""
def setup():
    """
    Setup the site
    """
    _confirm_branch()

    if env.strategy == 'git':
        git_clone_repo()
        git_checkout()
    elif env.strategy == 'svn':
        svn_checkout()

def deploy():
    """
    Deploy new code to the site
    """
    _confirm_branch()
    
    require('settings', provided_by=[production, staging])
    
    if env.strategy == 'git':
        require('gitbranch', provided_by=[stable, master, branch])

        git_checkout()

    elif env.strategy == 'svn':
        svn_checkout()

    sync_app_servers()

"""
Commands - data
"""
def bootstrap():
    with cd(env.path):
        print("\nStep 1: Install required PHP extensions/apps")

        if confirm('Continue installing requirements? Can skip if already installed.'):
            env.run('sudo ./requirements.sh')

        print("\nStep 2: Database and basic Wordpress setup")

        env.run(env.prefix + 'php tools/wp-scripts/setup_wp-config.php')

        create_db()
        env.run(env.prefix + 'php tools/wp-scripts/setup.php')

        env.run(env.prefix + 'php tools/wp-scripts/setup_wp-config.php --finish')

        print("\nStep 3: Setup plugins")

        env.run(env.prefix + 'php tools/wp-scripts/setup_plugins.php')

        print("\nStep 4: Cleanup, create blogs")

        env.run(env.prefix + 'php tools/wp-scripts/setup_root.php')

        if confirm("Create child blogs?"): create_blogs()

def create_db():
    if not env.db_root_pass:
        env.db_root_pass = getpass("Database password: ")

    if env.db_host == 'localhost':
        env.run('mysqladmin --user=%(db_root_user)s --password=%(db_root_pass)s create %(db_name)s' % env)
        env.run('echo "GRANT ALL ON * TO \'%(db_wpuser_name)s\'@\'localhost\' IDENTIFIED BY \'%(db_wpuser_pass)s\';" | mysql --user=%(db_root_user)s --password=%(db_root_pass)s %(db_name)s' % env)
    else:
        env.run('mysqladmin --host=%(db_host)s --user=%(db_root_user)s --password=%(db_root_pass)s create %(db_name)s' % env)
        env.run('echo "GRANT ALL ON * TO \'%(db_wpuser_name)s\'@\'%%\' IDENTIFIED BY \'%(db_wpuser_pass)s\';" | mysql --host=%(db_host)s --user=%(db_root_user)s --password=%(db_root_pass)s %(db_name)s' % env)

def load_db(dump_slug='dump'):
    env.dump_slug = dump_slug
    if not env.db_root_pass:
        env.db_root_pass = getpass("Database password: ")
    with cd(env.path):
        env.run("bzcat data/%(dump_slug)s.sql.bz2 |sed s/WPDEPLOYDOMAN/%(wpdomain)s/g |mysql --host=%(db_host)s --user=%(db_root_user)s --password=%(db_root_pass)s --max_allowed_packet=2M %(db_name)s" % env)

def dump_db(dump_slug='dump'):
    env.dump_slug = dump_slug
    if not env.db_root_pass:
        env.db_root_pass = getpass("Database password: ")
    with cd(env.path):
        env.run("mysqldump --host=%(db_host)s --user=%(db_root_user)s --password=%(db_root_pass)s --max_allowed_packet=2M --extended-insert=FALSE --lock-all-tables %(project_name)s |sed s/%(wpdomain)s/WPDEPLOYDOMAN/g |bzip2 > data/%(dump_slug)s.sql.bz2" % env)

def destroy_db():
    if not env.db_root_pass:
        env.db_root_pass = getpass("Database password: ")

    with settings(warn_only=True):
        if env.db_host == 'localhost':
            env.run('mysqladmin -f --user=%(db_root_user)s --password=%(db_root_pass)s drop %(project_name)s' % env)
            env.run('echo "DROP USER \'%(db_wpuser_name)s\'@\'localhost\';" | mysql --user=%(db_root_user)s --password=%(db_root_pass)s' % env)
        else:
            env.run('mysqladmin -f --host=%(db_host)s --user=%(db_root_user)s --password=%(db_root_pass)s drop %(project_name)s' % env)
            env.run('echo "DROP USER \'%(db_wpuser_name)s\'@\'%%\';" | mysql --host=%(db_host)s --user=%(db_root_user)s --password=%(db_root_pass)s' % env)
    
def destroy_attachments():
    with cd(env.path):
        env.run('rm -rf wp-content/blogs.dir')

def reload_db(dump_slug='dump'):
    destroy_db()
    create_db()
    env.run(env.prefix + 'php %(path)s/wp-scripts/setup_wp-config.php --finish' % env )
    load_db(dump_slug)
    # run_scripts()

def create_blogs():
    i=0;
    response='';
    while "No more blogs" not in response:
        response = env.run(env.prefix + "php wp-scripts/setup_blog.php -n %s" % i)
        i+=1;

def force_nfs_refresh():
    env.run("run-for-cluster -t app 'cd %(path)s; git status;'" % env)

def sync_app_servers():
    env.run("run-for-cluster -t app 'sudo rsync -a --delete /mnt/apps/sites/%(project_name)s/ %(path)s/'" % env)

def fix_perms():
    if env.fix_perms:
        with cd(env.path):
            env.sudo("chgrp -Rf www-data media")
            env.sudo("chmod -Rf g+rw media")

def wrap_media():
    with cd(env.path):
        env.run('tar zcf data/media.tgz wp-content/blogs.dir/* wp-content/uploads/*')
    print('Wrapped up media.\n')

def unwrap_media():
    with cd(env.path):
        env.run('tar zxf data/media.tgz')
    print('Unwrapped media.\n')

def put_media():
    check_env()
    put('data/media.tgz','%(path)s/data/media.tgz' % env)
    print('Put media on server.\n')

def get_media():
    check_env()
    get('%(path)s/data/media.tgz' % env, 'data/media.tgz')
    print('Got media from the server.\n')

"""
Deaths, destroyers of worlds
"""
def shiva_the_destroyer():
    """
    Remove all directories, databases, etc. associated with the application.
    """
    try:
        env.settings
        check_env()
        env.run('rm -Rf %(path)s;' % env)
        destroy_db()
    except AttributeError, e:
        with settings(warn_only=True):
            env.run('rm .htaccess')
            env.run('rm wp-config.php')
        destroy_db()

"""
Utilities
"""
def check_env():
    require('settings', provided_by=[production, staging])
    env.sudo = sudo
    env.run = run

def runserver():
    username = getuser()
    local("sudo ./tools/bin/runserver.py %s" % username, capture=False)

def _confirm_branch():
    if (env.settings == 'production' and env.gitbranch != 'stable'):
        answer = prompt("You are trying to deploy the '%(gitbranch)s' branch to production.\nYou should really only deploy a stable branch.\nDo you know what you're doing?" % env, default="Not at all")
        if answer not in ('y','Y','yes','Yes','buzz off','screw you'):
            exit()

"""
Project specific commands
"""
def clear_cache():
    require('settings', provided_by=[production,staging])
    for server in env.cache_servers:
        env.run('curl -X PURGE -H "Host: %s" http://%s/' % (env.wpdomain, server))

def clear_asset_cache():
    require('settings', provided_by=[production,staging])
    for server in env.cache_servers:
        env.run('curl -X PURGE -H "Host: %s" http://%s/.*/wp-content/.*' % (env.wpdomain, server))

def clear_admin_cache():
    require('settings', provided_by=[production,staging])
    for server in env.cache_servers:
        env.run('curl -X PURGE -H "Host: %s" http://%s/.*/wp-admin/.*' % (env.wpdomain, server))

def run_script(script_name):
    """
    Run a script in the /wp-scripts/ directory.
    """
    env.script_name = script_name
    with cd(env.path):
        env.run(env.prefix + 'php tools/wp-scripts/%(script_name)s.php' % env)

def robots_setup():
    require('settings', provided_by=[production, staging])
    
    with cd(env.path):
        env.run('cp robots_%(settings)s.txt robots.txt')