#!/bin/env python

from urllib.request import Request, urlopen
from urllib.parse import urlencode
import requests
import http.client
import os
import sys
from datetime import datetime
import time
import json
from collections import namedtuple
from typing import Any, List, Dict, Callable, TypeVar, Tuple
from bisect import bisect_right
import random
from pprint import pformat
import argparse

# http.client.HTTPConnection.debuglevel = 1

# /sync|buy-upgrade|upgrades-for-buy/
# interludeUser.lastSyncUpdate ~ int(datetime.now().timestamp())

# pph = profit per hour
# pp = payback period
Upgrade = namedtuple(
    'Upgrade', ['id', 'name', 'section', 'cooldown', 'price', 'pph', 'pp', 'available', 'condition', 'expiresAt'])

maxPP = 1000
minBalance = 5
maxIdle = 60 * 60 * 3  # ping every 3 hours to resume income


def currentTime() -> float:
    return datetime.now().timestamp()


def humanNumber(n: float) -> str:
    if n < 1000:
        return f'{n:.2f}'

    n /= 1000
    if n < 1_000:
        return f'{n:.2f}k'

    n /= 1_000
    if n < 1_000:
        return f'{n:.2f}M'

    n /= 1_000
    if n < 1_000:
        return f'{n:.2f}B'

    n /= 1_000
    return f'{n:.2f}T'


def formatTime(t: float) -> str:
    t = int(t + 0.5)

    f = f'{t % 60:02.0f}'
    t = int(t // 60)
    if t == 0:
        return f

    f = f'{t % 60:02d}:' + f
    t = t // 60
    if t == 0:
        return f

    f = f'{t % 24}:' + f
    t = t // 24
    if t == 0:
        return f

    return f'{t}d ' + f


class Tasks:
    def __init__(self):
        self.tasks = []

    def add(self, timePoint: float, name: str, task: Callable[[], Any]):
        ip = bisect_right(self.tasks, timePoint, key=lambda x: x[0])
        self.tasks.insert(ip, (timePoint, name, task))

    def exec(self):
        while True:
            if len(self.tasks) == 0:
                break

            timePoint, name, task = self.tasks.pop(0)

            now = currentTime()
            delta = timePoint - now
            if sys.stdout.isatty():
                while timePoint > now:
                    d = timePoint - now
                    print(f'\rWaiting {formatTime(d)}'
                          f' / {formatTime(delta)}'
                          f': {name}\033[K', end='')

                    if d > 120:
                        rate = 60
                    else:
                        rate = 1

                    if d > rate:
                        time.sleep(rate)
                        now = currentTime()
                    else:
                        print(f'\rWaiting {formatTime(delta)}: {name}\033[K')
                        time.sleep(d)
                        break
            else:
                if delta > 0:
                    print(f'Waiting {formatTime(delta)}: {name}')
                    time.sleep(delta)

            task()


T = TypeVar('T')

tasks = Tasks()

configFile = os.path.expanduser('~/.hk_bot.json')


def loadConfig() -> Dict:
    try:
        with open(configFile) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def saveConfig(config: Dict):
    with open(configFile, 'w') as f:
        json.dump(config, f)


def updateConfig(config: Dict, patch: Dict):
    for k, v in patch.items():
        config[k] = v
    saveConfig(config)


def post(request: str, body: Dict | None = None) -> Dict:
    url = 'https://api.hamsterkombatgame.io/interlude/' + request
    headers = {
        'authorization': os.environ['HK_AUTH'],
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
    }

    response = requests.post(url=url, json=body, headers=headers)

    try:
        r = json.loads(response.content)
    except:
        print(f'Response: {response.content}')
        raise

    if response.status_code != 200:
        raise Exception(f'Request failed with code {response.status_code}:\n'
                        f'{pformat(r)}')

    return r


def randomizeTime(timePoint: float, maxTimePoint: float | None = None) -> float:
    now = currentTime()
    delay = 3 + max(180, min(timePoint - now, 3600)) / 60 * random.random()
    if maxTimePoint is not None:
        delay = min(delay, max(0, maxTimePoint - timePoint - 5))
    return timePoint + delay


def is_available(u: Upgrade, all_upgrades: Dict[str, Upgrade]):
    while u is not None:
        if u.condition is None:
            return True
        u = all_upgrades.get(u.condition)

    return False


def sortUpgrades(upgradesForBuy: List[Dict]) -> List[Upgrade]:
    all_upgrades: Dict[str, Upgrade] = {}

    for u in upgradesForBuy:
        # filter upgrade
        if u['isExpired']:
            continue

        id = u['id']

        if 'maxLevel' in u:
            maxLevel = u['maxLevel']
            try:
                level = u['level']
            except KeyError:
                print(f"Can't figure out level for {id}, falling back to upgrades")
                level = upgradesForBuy['interludeUser']['upgrades'][id]['level'] + 1
            if level > maxLevel:
                continue

        condition = None
        try:
            cond = u['condition']
            if cond is not None:
                type = cond['_type']
                if type == 'ByUpgrade':
                    condition = cond['upgradeId']
                elif type in ('ReferralCount', 'MoreReferralsCount'):
                    continue
        except KeyError:
            pass

        # cooldown
        cooldown = 0
        try:
            cooldown = int(u['cooldownSeconds'])
        except KeyError:
            pass

        # price and payback period
        price = u['price']
        pph = u['profitPerHourDelta']

        if pph != 0:
            pp = price / pph
        else:
            pp = float('inf')

        # other
        available = u['isAvailable']

        expiresAt = None
        try:
            expiresAt = datetime.fromisoformat(u['expiresAt']).timestamp()
        except KeyError:
            pass

        all_upgrades[id] = Upgrade(
            id, u['name'], u['section'], cooldown, price, pph, pp, available, condition, expiresAt)

    sorted: List[Upgrade] = []
    for id, u in all_upgrades.items():
        if is_available(u, all_upgrades):
            sorted.append(u)

    sorted.sort(key=lambda u: u.pp)

    return sorted


def reportState(config: Dict):
    user = config['interludeUser']
    balance = user["balanceDiamonds"]
    earnPassivePerSec = user['earnPassivePerSec']
    lastSyncUpdate = user['lastSyncUpdate']
    now = currentTime()

    balance = balance
    if now > lastSyncUpdate:
        balance += earnPassivePerSec * (now - lastSyncUpdate)

    print(f'Balance: {humanNumber(balance)}'
          f', +{humanNumber(user["earnPassivePerHour"])}/h')


def chooseUpgrade(config: Dict, upgrades: List[Upgrade], quiet: bool = False) -> Tuple[Upgrade | None, float]:
    user = config['interludeUser']
    balance = user['balanceDiamonds']
    lastSyncUpdate = user['lastSyncUpdate']
    earnPassivePerSec = user['earnPassivePerSec']

    now = currentTime()

    upgrade = None
    timeToBuy = 0
    for u in upgrades:
        if not u.available:
            if not quiet:
                print(f'Skip {u.section} / {u.name} - not available')
            continue

        so = maxPP is not None and u.pp > maxPP  # second order
        deltaCoins = u.price - balance
        if so:
            deltaCoins += minBalance

        tob = lastSyncUpdate + deltaCoins / earnPassivePerSec  # time of balance

        ttb = max((
            now,
            tob,
            lastSyncUpdate + u.cooldown
        ))

        if u.expiresAt is not None:
            if ttb > u.expiresAt:
                if not quiet:
                    print(f'Skip {u.section} / {u.name} - expired')
                continue

        if upgrade is None or ttb < timeToBuy:
            upgrade = u
            timeToBuy = ttb

        balance -= u.price

        if timeToBuy <= now:
            break

    return upgrade, timeToBuy



def listUpgrades(config: Dict, maxItems: int = 20):
    reportState(config)

    upgrades = sortUpgrades(config['upgradesForBuy'])
    upgrade, timeToBuy = chooseUpgrade(config, upgrades, quiet=True)
    if maxItems is not None and maxItems > 0:
        upgrades = upgrades[:maxItems]

    user = config['interludeUser']
    lastSyncUpdate = user['lastSyncUpdate']
    now = currentTime()
    timePassed = now - lastSyncUpdate

    for u in upgrades:
        s_condition = '* ' if not u.available else ''

        cd = max(0, u.cooldown - timePassed)
        s_cd = f' (cd: {formatTime(cd)})' if cd > 0 else ''

        if u == upgrade:
            s_cur = ' <- buy '
            if timeToBuy > now:
                s_cur += f'in {formatTime(timeToBuy - now)}'
            else:
                s_cur += 'now'
        else:
            s_cur = ''

        print(f"{s_condition}{u.pp:.2f}h"
              f": {u.section} / {u.name} for {humanNumber(u.price)}{s_cd}{s_cur}")


def buy(upgrade: Upgrade, config: Dict):
    print(f'Buy {upgrade.name}')
    response = post(
        'buy-upgrade',
        {
            'upgradeId': upgrade.id,
            'timestamp': int(currentTime() * 1000)
        }
    )

    updateConfig(config, response)
    reportState(config)


def scheduleBuy(config: Dict, tasks: Tasks):
    user = config['interludeUser']
    lastSyncUpdate = user['lastSyncUpdate']

    timeToSync = lastSyncUpdate + maxIdle

    upgrades = sortUpgrades(config['upgradesForBuy'])
    upgrade, timeToBuy = chooseUpgrade(config, upgrades)

    def forceSync():
        print('Sync')
        updateConfig(config, post('sync'))
        updateConfig(config, post('upgrades-for-buy'))
        reportState(config)
        scheduleBuy(config, tasks)

    if upgrade is None:
        tasks.add(randomizeTime(timeToSync), 'idle', forceSync)
        return

    def recur():
        buy(upgrade, config)
        scheduleBuy(config, tasks)

    print(f'{"Wait" if timeToSync < timeToBuy else "Prepare"} to buy {upgrade.section} / {upgrade.name}'
          f' for {humanNumber(upgrade.price)}'
          f', +{humanNumber(upgrade.pph)}/h'
          f', pp = {upgrade.pp:.2f}h')

    if timeToSync < timeToBuy:
        tasks.add(randomizeTime(timeToSync), 'keep alive', forceSync)
    else:
        tasks.add(randomizeTime(timeToBuy, upgrade.expiresAt), 'buy', recur)


def main():
    parser = argparse.ArgumentParser(prog='bot')
    parser.add_argument('-c', '--clear', action='store_true',
                        help='ignore saved state')
    parser.add_argument('-l', '--list', action='store_true',
                        help='list upgrades')

    args = parser.parse_args()

    if not args.clear:
        config = loadConfig()
    else:
        config = {}

    if 'interludeUser' not in config or 'upgradesForBuy' not in config:
        print('Initial sync')
        updateConfig(config, post('sync'))
        updateConfig(config, post('upgrades-for-buy'))

    if args.list:
        listUpgrades(config)
        return

    reportState(config)

    scheduleBuy(config, tasks)

    tasks.exec()


if __name__ == '__main__':
    main()
