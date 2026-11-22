from fractions import Fraction
from .checks import number

def step(tanks,transfers,species):
    incoming={t:Fraction(0) for t in tanks};outgoing=dict(incoming)
    delta={t:{s:Fraction(0) for s in species} for t in tanks};evidence=[]
    for transfer in transfers:
        source,target=transfer['source'],transfer['target'];volume=number(transfer['volume_l'])
        outgoing[source]+=volume;incoming[target]+=volume
    for name,tank in tanks.items():
        if outgoing[name]>tank['volume']:raise ValueError(f'opening volume insufficient: {name}')
        closing=tank['volume']-outgoing[name]+incoming[name]
        if closing>tank['capacity']:raise ValueError(f'closing capacity exceeded: {name}')
    for transfer in transfers:
        source,target=transfer['source'],transfer['target'];volume=number(transfer['volume_l']);tank=tanks[source]
        masses={s:tank['masses'][s]*volume/tank['volume'] for s in species}
        for s,mass in masses.items():delta[source][s]-=mass;delta[target][s]+=mass
        evidence.append({'transfer_id':transfer['id'],'source':source,'target':target,'volume_l':str(volume),'mass_g':{s:str(v) for s,v in masses.items()}})
    result={}
    for name,tank in tanks.items():
        result[name]={'capacity':tank['capacity'],'volume':tank['volume']-outgoing[name]+incoming[name],
                      'masses':{s:tank['masses'][s]+delta[name][s] for s in species}}
        if any(m<0 for m in result[name]['masses'].values()):raise ValueError('internal negative mass')
    return result,evidence,incoming,outgoing
