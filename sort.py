import json
from collections import namedtuple
from typing import List, Dict

# pph = profit per hour
# pp = payback period
Upgrade = namedtuple(
    'Upgrade', ['name', 'section', 'cooldown', 'price', 'pph', 'pp', 'available', 'condition'])

all_upgrades: Dict[str, Upgrade] = {}


def is_available(u: Upgrade):
    while u is not None:
        if u.condition is None:
            return True
        u = all_upgrades.get(u.condition)

    return False


# /sync|buy-upgrade|upgrades-for-buy/
# with open('test') as f:
with open('upgradesForBuy') as f:
    upgrades = json.load(f)
    for u in upgrades:
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

        all_upgrades[u['id']] = Upgrade(
            u['name'], u['section'], cooldown, price, pph, pp, available, condition)


sorted: List[Upgrade] = []
for id, u in all_upgrades.items():
    if is_available(u):
        sorted.append(u)

sorted.sort(key=lambda u: u.pp)

for u in sorted[:20]:
    condition = '* ' if not u.available else ''
    cd = f" (cd: {u.cooldown}s)" if u.cooldown > 0 else ""
    print(f"{condition}{u.section} / {u.name} : {u.pp:.2f}h{cd}")
