from gavel import app
from gavel.models import *
from gavel.constants import *
import gavel.settings as settings
import gavel.utils as utils
import gavel.crowd_bt as crowd_bt
from flask import (
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func
from numpy.random import choice, random, shuffle
from functools import wraps
from datetime import datetime
import time

def requires_open(redirect_to):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if Setting.value_of(SETTING_CLOSED) == SETTING_TRUE:
                return redirect(url_for(redirect_to))
            else:
                return f(*args, **kwargs)
        return decorated
    return decorator

def requires_active_annotator(redirect_to):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            annotator = get_current_annotator()
            if annotator is None or not annotator.active:
                return redirect(url_for(redirect_to))
            else:
                return f(*args, **kwargs)
        return decorated
    return decorator


@app.route('/')
def index():
    annotator = get_current_annotator()
    if annotator is None:
        return render_template(
            'logged_out.html',
            content=utils.render_markdown(settings.LOGGED_OUT_MESSAGE)
        )
    else:
        if Setting.value_of(SETTING_CLOSED) == SETTING_TRUE:
            return render_template(
                'closed.html',
                content=utils.render_markdown(settings.CLOSED_MESSAGE)
            )
        if not annotator.active:
            return render_template(
                'disabled.html',
                content=utils.render_markdown(settings.DISABLED_MESSAGE)
            )
        if not annotator.read_welcome:
            return redirect(url_for('welcome'))
        maybe_init_annotator(annotator)
        if annotator.next is None:
            return render_template(
                'wait.html',
                content=utils.render_markdown(settings.WAIT_MESSAGE)
            )
        elif annotator.prev is None:
            time = Setting.by_key("start_time").value
            return render_template('begin.html', start_time=time, item=annotator.next)
        else:
            query = Decision.query.filter_by(annotator_id=annotator.id)
            if query.count() > 0:
                time = max(Decision.query.filter_by(annotator_id=annotator.id), key=lambda d: d.time).time
            else:
                time = Setting.by_key("start_time").value
            return render_template('vote.html', start_time=time, prev=annotator.prev, next=annotator.next)

@app.route('/vote', methods=['POST'])
@requires_open(redirect_to='index')
@requires_active_annotator(redirect_to='index')
def vote():
    annotator = get_current_annotator()
    if annotator.prev.id == int(request.form['prev_id']) and annotator.next.id == int(request.form['next_id']):
        if request.form['action'] == 'Skip':
            annotator.ignore.append(annotator.next)
        else:
            # ignore things that were deactivated in the middle of judging
            if annotator.prev.active and annotator.next.active:
                if request.form['action'] == 'Previous':
                    perform_vote(annotator, next_won=False)
                    decision = Decision(annotator, winner=annotator.prev, loser=annotator.next)
                elif request.form['action'] == 'Current':
                    perform_vote(annotator, next_won=True)
                    decision = Decision(annotator, winner=annotator.next, loser=annotator.prev)
                db.session.add(decision)
            annotator.next.viewed.append(annotator) # counted as viewed even if deactivated
            annotator.prev = annotator.next
            annotator.ignore.append(annotator.prev)
        annotator.update_next(choose_next(annotator))
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/begin', methods=['POST'])
@requires_open(redirect_to='index')
@requires_active_annotator(redirect_to='index')
def begin():
    annotator = get_current_annotator()
    if annotator.next.id == int(request.form['item_id']):
        annotator.ignore.append(annotator.next)
        if request.form['action'] == 'Done':
            annotator.next.viewed.append(annotator)
            annotator.prev = annotator.next
            annotator.update_next(choose_next(annotator))
        elif request.form['action'] == 'Skip':
            annotator.next = None # will be reset in index
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop(ANNOTATOR_ID, None)
    return redirect(url_for('index'))

@app.route('/login/<secret>/')
def login(secret):
    annotator = Annotator.by_secret(secret)
    if annotator is None:
        session.pop(ANNOTATOR_ID, None)
        session.modified = True
    else:
        session[ANNOTATOR_ID] = annotator.id
    return redirect(url_for('index'))

@app.route('/welcome/')
@requires_open(redirect_to='index')
@requires_active_annotator(redirect_to='index')
def welcome():
    return render_template(
        'welcome.html',
        content=utils.render_markdown(settings.WELCOME_MESSAGE)
    )

@app.route('/welcome/done', methods=['POST'])
@requires_open(redirect_to='index')
@requires_active_annotator(redirect_to='index')
def welcome_done():
    annotator = get_current_annotator()
    if request.form['action'] == 'Done':
        annotator.read_welcome = True
    db.session.commit()
    return redirect(url_for('index'))

def get_current_annotator():
    return Annotator.by_id(session.get(ANNOTATOR_ID, None))

def preferred_items(annotator, active_items=None, annotators=None):
    '''
    Return a list of preferred items for the given annotator to look at next.

    This method uses a variety of strategies to try to select good candidate
    projects.
    '''
    items = []
    ignored_ids = {i.id for i in annotator.ignore}

    if ignored_ids:
        available_items = Item.query.filter(
            (Item.active == True) & (~Item.id.in_(ignored_ids))
        ).all()
    else:
        available_items = active_items if active_items else Item.query.filter(Item.active == True).all()

    prioritized_items = [i for i in available_items if i.prioritized]

    items = prioritized_items if prioritized_items else available_items

    if not annotators: 
        annotators = Annotator.query.filter(
        (Annotator.active == True) & (Annotator.next != None) & (Annotator.updated != None)
    ).all()
    busy = {i.next.id for i in annotators if \
        (datetime.utcnow() - i.updated).total_seconds() < settings.TIMEOUT * 60}
    nonbusy = [i for i in items if i.id not in busy]
    preferred = nonbusy if nonbusy else items

    less_seen = [i for i in preferred if len(i.viewed) < settings.MIN_VIEWS]

    return less_seen if less_seen else preferred

def maybe_init_annotator(annotator):
    if annotator.next is None:
        items = preferred_items(annotator)
        if items:
            annotator.update_next(choice(items))
            db.session.commit()

def choose_next(annotator):
    recompute_estimates()
    items = preferred_items(annotator)

    shuffle(items) # useful for argmax case as well in the case of ties
    if items:
        if random() < crowd_bt.EPSILON:
            # update wait time to 0
            item = items[0]
            item.estimate = 0
            db.session.commit()
            return item
        else:
            return crowd_bt.argmax(lambda i: crowd_bt.expected_information_gain(
                annotator.alpha,
                annotator.beta,
                annotator.prev.mu,
                annotator.prev.sigma_sq,
                i.mu,
                i.sigma_sq), items)
    else:
        return None

def recompute_estimates():
    start = time.time()

    new_estimates = {}

    estimate_annotators = Annotator.query.filter(Annotator.prev_id != None).all()

    active_annotators = Annotator.query.filter(
        (Annotator.active == True) & (Annotator.next != None) & (Annotator.updated != None)
    ).all()

    active_items = Item.query.filter(Item.active == True).all()

    for annotator in estimate_annotators:
        items = preferred_items(annotator, active_items, active_annotators)
        # sort in descending order of expected information gain
        items.sort(key=lambda i: crowd_bt.expected_information_gain(
            annotator.alpha,
            annotator.beta,
            annotator.prev.mu,
            annotator.prev.sigma_sq,
            i.mu,
            i.sigma_sq), reverse=True)

        for idx, item in enumerate(items):
            if item.id not in new_estimates.keys():
                new_estimates[item.id] = idx * AVG_JUDGE_TIME
            else:
                new_estimates[item.id] = min(new_estimates[item.id], idx * AVG_JUDGE_TIME)

    db.session.commit()
    end = time.time()
    print(end-start)

def perform_vote(annotator, next_won):
    if next_won:
        winner = annotator.next
        loser = annotator.prev
    else:
        winner = annotator.prev
        loser = annotator.next
    u_alpha, u_beta, u_winner_mu, u_winner_sigma_sq, u_loser_mu, u_loser_sigma_sq = crowd_bt.update(
        annotator.alpha,
        annotator.beta,
        winner.mu,
        winner.sigma_sq,
        loser.mu,
        loser.sigma_sq
    )
    annotator.alpha = u_alpha
    annotator.beta = u_beta
    winner.mu = u_winner_mu
    winner.sigma_sq = u_winner_sigma_sq
    loser.mu = u_loser_mu
    loser.sigma_sq = u_loser_sigma_sq
