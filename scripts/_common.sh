#!/bin/bash

#=================================================
# COMMON VARIABLES AND CUSTOM HELPERS
#=================================================

# Copy the web app from the package (the repo root) into the install dir and build its virtualenv.
_install_app() {
    mkdir -p "$install_dir"
    cp "$YNH_APP_BASEDIR/server.py" "$YNH_APP_BASEDIR/monitor.py" "$YNH_APP_BASEDIR/solar.py" "$YNH_APP_BASEDIR/index.html" "$YNH_APP_BASEDIR/requirements.txt" "$install_dir/"

    ynh_safe_rm "$install_dir/venv"
    python3 -m venv "$install_dir/venv"
    "$install_dir/venv/bin/pip" install --quiet --upgrade pip
    "$install_dir/venv/bin/pip" install --quiet -r "$install_dir/requirements.txt"

    chown -R "$app:www-data" "$install_dir"
    chmod -R o-rwx "$install_dir"
}

# The saved iCOMM login lives here, so keep it private to the app.
_setup_dirs() {
    mkdir -p "/var/log/$app"
    chown -R "$app:$app" "/var/log/$app" "$data_dir"
    chmod 750 "$data_dir"
}
