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

# http.client.HTTPConnection.debuglevel = 1

# /sync|buy-upgrade|upgrades-for-buy/
# clickerUser.lastSyncUpdate ~ int(datetime.now().timestamp())
maxPP = 2000
minBalance = 50_000_000


def formatCoins(n: float) -> str:
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


# pph = profit per hour
# pp = payback period
Upgrade = namedtuple(
    'Upgrade', ['id', 'name', 'section', 'cooldown', 'price', 'pph', 'pp', 'available', 'condition'])


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

        price = int(u['price'])
        pph = int(u['profitPerHourDelta'])

        if pph != 0:
            pp = price / pph
        else:
            pp = float('inf')

        available = u['isAvailable']
        id = u['id']

        all_upgrades[id] = Upgrade(
            id, u['name'], u['section'], cooldown, price, pph, pp, available, condition)

    sorted: List[Upgrade] = []
    for id, u in all_upgrades.items():
        if is_available(u, all_upgrades):
            sorted.append(u)

    sorted.sort(key=lambda u: u.pp)

    return sorted


def post(request: str, body: Dict | None = None) -> Dict:
    url = 'https://api.hamsterkombatgame.io/clicker/' + request
    headers = {
        'authorization': os.environ['HK_AUTH'],
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
    }

    response = requests.post(url=url, json=body, headers=headers)

    r = json.loads(response.content)

    if response.status_code != 200:
        raise Exception(f'Request failed with code {response.status_code}:\n'
                        f'{pformat(r)}')

    return r


def reportState(config: Dict):
    clickerUser = config['clickerUser']
    print(f'Balance: {formatCoins(clickerUser["balanceCoins"])}'
          f', +{formatCoins(clickerUser["earnPassivePerHour"])}/h')


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


def rndDelay() -> float:
    return 5 + 55 * random.random()


def scheduleBuy(config: Dict, tasks: Tasks):
    # ping every 3 hours to resume income
    maxIdle = 60 * 60 * 3  # 3 hours

    clickerUser = config['clickerUser']
    balanceCoins = clickerUser['balanceCoins']
    lastSyncUpdate = clickerUser['lastSyncUpdate']
    earnPassivePerHour = clickerUser['earnPassivePerHour']
    earnPassivePerSec = clickerUser['earnPassivePerSec']

    now = datetime.now().timestamp()
    timeAfterSync = now - lastSyncUpdate
    timeToSync = lastSyncUpdate + maxIdle

    upgrades = sortUpgrades(config['upgradesForBuy'])
    upgrade = None
    cooldown = 0
    secondOrder = False
    for u in upgrades:
        if not u.available:
            print(f'Skip {u.section} / {u.name} - not available')
            continue

        so = maxPP is not None and u.pp > maxPP
        deltaCoins = u.price - balanceCoins
        deltaCoins *= 1.1 + so * minBalance

        timeOfBalance = lastSyncUpdate + deltaCoins / earnPassivePerSec

        cd = max((
            0,
            timeOfBalance - now,
            u.cooldown - timeAfterSync
        ))

        if upgrade is None or (not secondOrder and cd < cooldown):
            upgrade = u
            cooldown = cd
            secondOrder = so
        else:
            break

    def forceSync():
        print('Sync')
        updateConfig(config, post('sync'))
        updateConfig(config, post('upgrades-for-buy'))
        reportState(config)
        scheduleBuy(config, tasks)

    if upgrade is None:
        tasks.add(timeToSync - now + rndDelay(), 'idle', forceSync)
        return

    def recur():
        buy(upgrade, config)
        scheduleBuy(config, tasks)

    keepAliveDelay = max(0, timeToSync - now)

    print(f'{"Wait" if keepAliveDelay < cooldown else "Prepare"} to buy {upgrade.section} / {upgrade.name}'
          f' for {formatCoins(upgrade.price)}'
          f', +{formatCoins(upgrade.pph)}/h'
          f', pp = {upgrade.pp:.2f}h')

    if keepAliveDelay < cooldown:
        tasks.add(keepAliveDelay + rndDelay(), 'keep alive', forceSync)
    else:
        tasks.add(cooldown + rndDelay(), 'buy', recur)


def main():
    config = loadConfig()

    if 'clickerUser' not in config:
        updateConfig(config, post('sync'))

    if 'upgradesForBuy' not in config:
        updateConfig(config, post('upgrades-for-buy'))

    reportState(config)

    scheduleBuy(config, tasks)

    tasks.exec()


if __name__ == '__main__':
    main()
