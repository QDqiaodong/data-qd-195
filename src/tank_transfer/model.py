from .checks import array,fields,ident,number,unique

def validate(data):
    fields(data,('species','tanks','steps'))
    species=array(data['species'],1,20)
    for s in species:ident(s)
    unique(species)
    tanks={}
    for tank in array(data['tanks'],1,100):
        fields(tank,('id','capacity_l','volume_l','mass_g'));name=ident(tank['id'])
        if name in tanks:raise ValueError('duplicate tank')
        capacity,volume=number(tank['capacity_l']),number(tank['volume_l'])
        if capacity<=0 or not 0<=volume<=capacity:raise ValueError('invalid tank capacity or volume')
        masses=tank['mass_g']
        if not isinstance(masses,dict) or set(masses)!=set(species):raise ValueError('every species mass required')
        masses={s:number(masses[s]) for s in species}
        if any(v<0 for v in masses.values()) or (volume==0 and any(masses.values())):raise ValueError('invalid initial species mass')
        tanks[name]={'capacity':capacity,'volume':volume,'masses':masses}
    steps=array(data['steps'],0,1000);count=0
    for step in steps:
        fields(step,('id','transfers'));ident(step['id'])
        transfers=array(step['transfers'],0,1000);count+=len(transfers)
        for t in transfers:
            fields(t,('id','source','target','volume_l'));ident(t['id']);ident(t['source']);ident(t['target'])
            if t['source'] not in tanks or t['target'] not in tanks or t['source']==t['target']:raise ValueError('unknown or identical transfer endpoints')
            if number(t['volume_l'])<=0:raise ValueError('positive transfer volume required')
        unique([t['id'] for t in transfers])
    unique([s['id'] for s in steps])
    if count>10000 or len(steps)*len(tanks)*len(species)>1000000:raise ValueError('simulation work limit exceeded')
    return species,tanks,steps
