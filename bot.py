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
from typing import Any, List, Dict, Callable, TypeVar
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
safetyDelay = 60


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

    def add(self, delay: float, name: str, task: Callable[[], Any]):
        now = datetime.now().timestamp()
        timePoint = now + delay
        ip = bisect_right(self.tasks, timePoint, key=lambda x: x[0])
        self.tasks.insert(ip, (timePoint, name, task))

    def exec(self):
        while True:
            if len(self.tasks) == 0:
                break

            timePoint, name, task = self.tasks.pop(0)

            delta = timePoint - datetime.now().timestamp()
            if sys.stdout.isatty():
                while True:
                    d = timePoint - datetime.now().timestamp()
                    print(f'\rWaiting {formatTime(d)}'
                          f' / {formatTime(delta)}'
                          f': {name}\033[K', end='')

                    if d > 120:
                        rate = 60
                    else:
                        rate = 1

                    if d > rate:
                        time.sleep(rate)
                    else:
                        print(f'\rWaiting {formatTime(delta)}: {name}\033[K')
                        if d > 0:
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


def is_available(u: Upgrade, all_upgrades: Dict[str, Upgrade]):
    while u is not None:
        if u.condition is None:
            return True
        u = all_upgrades.get(u.condition)

    return False


def sortUpgrades(upgradesForBuy: List[Dict]) -> List[Upgrade]:
    all_upgrades: Dict[str, Upgrade] = {}

    for u in upgradesForBuy:
        if u['isExpired']:
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

        cooldown = 0
        try:
            cooldown = int(u['cooldownSeconds'])
        except KeyError:
            pass

        price = u['price']
        pph = u['profitPerHourDelta']

        if pph != 0:
            pp = price / pph
        else:
            pp = float('inf')

        available = u['isAvailable']
        id = u['id']

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


def reportState(config: Dict):
    interludeUser = config['interludeUser']
    balanceDiamonds = interludeUser["balanceDiamonds"]
    earnPassivePerSec = interludeUser['earnPassivePerSec']
    lastSyncUpdate = interludeUser['lastSyncUpdate']
    now = datetime.now().timestamp()

    balance = balanceDiamonds
    if now > lastSyncUpdate:
        balance += earnPassivePerSec * (now - lastSyncUpdate)

    print(f'Balance: {humanNumber(balance)}'
          f', +{humanNumber(interludeUser["earnPassivePerHour"])}/h')


def listUpgrades(config: Dict, maxItems: int = 20):
    reportState(config)
    upgrades = sortUpgrades(config['upgradesForBuy'])
    if maxItems is not None and maxItems > 0:
        upgrades = upgrades[:maxItems]

    interludeUser = config['interludeUser']
    lastSyncUpdate = interludeUser['lastSyncUpdate']
    now = datetime.now().timestamp()
    timePassed = now - lastSyncUpdate

    for u in upgrades:
        s_condition = '* ' if not u.available else ''
        cd = max(0, u.cooldown - timePassed)
        s_cd = f" (cd: {formatTime(cd)})" if cd > 0 else ""
        print(f"{s_condition}{u.pp:.2f}h"
              f": {u.section} / {u.name} for {humanNumber(u.price)}{s_cd}")


def buy(upgrade: Upgrade, config: Dict):
    print(f'Buy {upgrade.name}')
    response = post(
        'buy-upgrade',
        {
            'upgradeId': upgrade.id,
            'timestamp': int(datetime.now().timestamp() * 1000)
        }
    )

    updateConfig(config, response)
    reportState(config)


def randomizeDelay(delay: float) -> float:
    return 3 + min(3600, max(180, delay)) / 60 * random.random()


def scheduleBuy(config: Dict, tasks: Tasks):
    # ping every 3 hours to resume income
    maxIdle = 60 * 60 * 3  # 3 hours

    interludeUser = config['interludeUser']
    balanceDiamonds = interludeUser['balanceDiamonds']
    lastSyncUpdate = interludeUser['lastSyncUpdate']
    earnPassivePerHour = interludeUser['earnPassivePerHour']
    earnPassivePerSec = interludeUser['earnPassivePerSec']

    now = datetime.now().timestamp()
    timeToSync = lastSyncUpdate + maxIdle

    upgrades = sortUpgrades(config['upgradesForBuy'])
    upgrade = None
    cooldown = 0
    secondOrder = False
    delay = None
    for u in upgrades:
        if not u.available:
            print(f'Skip {u.section} / {u.name} - not available')
            continue

        so = maxPP is not None and u.pp > maxPP
        deltaCoins = u.price - balanceDiamonds
        if so:
            deltaCoins += minBalance

        timeOfBalance = lastSyncUpdate + deltaCoins / earnPassivePerSec
        timeOfBalance += safetyDelay

        timeToBuy = max((
            now,
            timeOfBalance,
            lastSyncUpdate + u.cooldown
        ))

        cd = timeToBuy - now
        d = randomizeDelay(cd)
        if u.expiresAt is not None:
            if timeToBuy > u.expiresAt:
                print(f'Skip {u.section} / {u.name} - expired')
                continue

            maxDelay = max(0, u.expiresAt - timeToBuy - 5)
            d = min(d, maxDelay)

        if upgrade is None or (not so and cd < cooldown):
            upgrade = u
            cooldown = cd
            secondOrder = so
            delay = d
        else:
            break

    def forceSync():
        print('Sync')
        updateConfig(config, post('sync'))
        updateConfig(config, post('upgrades-for-buy'))
        reportState(config)
        scheduleBuy(config, tasks)

    if upgrade is None:
        tasks.add(timeToSync - now +
                  randomizeDelay(timeToSync - now), 'idle', forceSync)
        return

    def recur():
        buy(upgrade, config)
        scheduleBuy(config, tasks)

    keepAliveDelay = max(0, timeToSync - now)

    print(f'{"Wait" if keepAliveDelay < cooldown else "Prepare"} to buy {upgrade.section} / {upgrade.name}'
          f' for {humanNumber(upgrade.price)}'
          f', +{humanNumber(upgrade.pph)}/h'
          f', pp = {upgrade.pp:.2f}h')

    if keepAliveDelay < cooldown:
        tasks.add(keepAliveDelay + delay, 'keep alive', forceSync)
    else:
        tasks.add(cooldown + delay, 'buy', recur)


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
