import os
import logging
import datetime
import bcrypt

from functools import wraps
from flask import redirect
from flask import session
from flask import g
from sqlalchemy.exc import NoResultFound
from password_strength import PasswordPolicy

from hm_pyhelper.miner_param import get_ethernet_addresses
from hm_pyhelper.sbc import is_commercial_fleet
from hw_diag.database.models.auth import AuthKeyValue
from hw_diag.database.models.auth import AuthFailure


def authenticate(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect('/login')
        return f(*args, **kwargs)
    return wrapper


def commercial_fleet_only(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not is_commercial_fleet():
            return redirect('/upgrade')
        return f(*args, **kwargs)
    return wrapper


def generate_default_password():
    mac_addrs = {'E0': '', 'W0': ''}
    get_ethernet_addresses(mac_addrs)
    mac_address = mac_addrs.get('E0')

    if not mac_address:
        # No ethernet mac on this device, use wifi instead...
        mac_address = mac_addrs.get('W0')

    default_password = mac_address.replace(':', '')
    return default_password


def write_password(password):
    password = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password, salt)
    hashed_str = hashed.decode('utf-8')

    try:
        password_row = g.db.query(AuthKeyValue). \
            filter(AuthKeyValue.key == 'password_hash'). \
            one()
        password_row.value = hashed_str
    except NoResultFound:
        password_row = AuthKeyValue(
            key='password_hash',
            value=hashed_str
        )
        g.db.add(password_row)
    g.db.commit()

    return password_row


def read_password():
    PASSWORD_OVERRIDE = os.getenv('PASSWORD_OVERRIDE', 'false')
    if PASSWORD_OVERRIDE != "false":  # nosec
        default_password = PASSWORD_OVERRIDE
        password_row = write_password(default_password)
        logging.info("Using password from override env var!")
    else:
        try:
            password_row = g.db.query(AuthKeyValue). \
            filter(AuthKeyValue.key == 'password_hash'). \
            one()
        except NoResultFound:
            default_password = generate_default_password()
            password_row = write_password(default_password)
            logging.info("No password override. Generating default!")
    return password_row


def check_password(password):
    password_row = read_password()
    hashed_password = password_row.value.encode('utf-8')
    password = password.encode('utf-8')
    if bcrypt.checkpw(password, hashed_password):
        return True
    else:
        return False


def update_password(current_password, new_password, confirm_password):
    error = False
    msg = ''
    color = 'red'

    if not check_password(current_password):
        error = True
        msg = 'Current password is not valid.'
        color = 'red'

    if new_password != confirm_password:
        error = True
        msg = 'New password and password confirmation do not match.'
        color = 'red'

    policy = PasswordPolicy.from_names(
        length=8,  # min length: 8
        uppercase=1,  # need min. 2 uppercase letters
        numbers=1,  # need min. 2 digits
        special=1,  # need min. 2 special characters
        nonletters=0,  # need min. 2 non-letter characters (digits, specials, anything)
    )

    if len(policy.test(new_password)) > 0:
        error = True
        msg = (
            'Password is not complex enough, please ensure password is greater than 8 '
            'characters, has at least 1 number, 1 uppercase character and 1 special character.'
        )
        color = 'red'

    if not error:
        write_password(new_password)
        msg = 'Password updated successfully.'
        color = 'green'

    return {
        'error': error,
        'msg': msg,
        'color': color
    }


def count_recent_auth_failures(minutes=10):
    now = datetime.datetime.utcnow()
    dt = now - datetime.timedelta(minutes=minutes)
    failure_count = g.db.query(AuthFailure). \
        filter(AuthFailure.dt > dt). \
        count()
    return failure_count


def add_login_failure(ip):
    now = datetime.datetime.utcnow()
    auth_failure = AuthFailure(
        dt=now,
        ip=ip
    )
    g.db.add(auth_failure)
    g.db.commit()


def update_password_reset_expiry():
    now = datetime.datetime.utcnow()
    expiry = now + datetime.timedelta(minutes=1)

    try:
        reset_expiry_row = g.db.query(AuthKeyValue). \
            filter(AuthKeyValue.key == 'password_reset_expiry'). \
            one()
        reset_expiry_row.value = expiry.isoformat()
        g.db.commit()
    except NoResultFound:
        reset_expiry_row = AuthKeyValue(
            key='password_reset_expiry',
            value=expiry.isoformat()
        )
        g.db.add(reset_expiry_row)
        g.db.commit()


def set_last_password_reset():
    now = datetime.datetime.utcnow()
    try:
        reset_expiry_row = g.db.query(AuthKeyValue). \
            filter(AuthKeyValue.key == 'password_last_reset'). \
            one()
        reset_expiry_row.value = now.isoformat()
        g.db.commit()
    except NoResultFound:
        reset_expiry_row = AuthKeyValue(
            key='password_last_reset',
            value=now.isoformat()
        )
        g.db.add(reset_expiry_row)
        g.db.commit()


def password_updated_in_last_minute():
    now = datetime.datetime.utcnow()
    one_min_ago = now - datetime.timedelta(minutes=1)
    try:
        last_reset_row = g.db.query(AuthKeyValue). \
            filter(AuthKeyValue.key == 'password_last_reset'). \
            one()
        last_reset = datetime.datetime.fromisoformat(last_reset_row.value)

        if last_reset > one_min_ago:
            valid = True
        else:
            valid = False
    except Exception:
        valid = False

    return valid


def perform_password_reset():
    now = datetime.datetime.utcnow()

    try:
        reset_expiry_row = g.db.query(AuthKeyValue). \
            filter(AuthKeyValue.key == 'password_reset_expiry'). \
            one()
        expiry = datetime.datetime.fromisoformat(reset_expiry_row.value)

        if expiry > now:
            default_password = generate_default_password()
            write_password(default_password)
            set_last_password_reset()
            valid = True
        else:
            valid = False

    except Exception:
        valid = False

    return valid


def can_spawn_admin_session():
    try:
        admin_session_expires_row = g.db.query(AuthKeyValue). \
            filter(AuthKeyValue.key == 'admin_session_expires'). \
            one()
        now = datetime.datetime.utcnow()
        expiry = datetime.datetime.fromisoformat(admin_session_expires_row.value)
        if now < expiry:
            return True
        else:
            return False
    except NoResultFound:
        return False
