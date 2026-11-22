from fractions import Fraction
from .mixing import step
from .model import validate

def totals(tanks,species):
    return sum((t['volume'] for t in tanks.values()),Fraction(0)),{s:sum((t['masses'][s] for t in tanks.values()),Fraction(0)) for s in species}

def snapshot(tanks,species):
    return {name:{'volume_l':str(t['volume']),'mass_g':{s:str(t['masses'][s]) for s in species},
                  'concentration_g_per_l':{s:str(t['masses'][s]/t['volume']) if t['volume'] else None for s in species}} for name,t in sorted(tanks.items())}

def process(data):
    species,tanks,steps=validate(data)
    initial=snapshot(tanks,species);initial_volume,initial_mass=totals(tanks,species);trace=[]
    for command in steps:
        before=tanks;tanks,evidence,incoming,outgoing=step(tanks,command['transfers'],species)
        volume,mass=totals(tanks,species)
        if volume!=initial_volume or mass!=initial_mass:raise ValueError('internal global conservation failure')
        volume_residual={name:str(tanks[name]['volume']-before[name]['volume']-incoming[name]+outgoing[name]) for name in tanks}
        trace.append({'step_id':command['id'],'transfers':evidence,'closing':snapshot(tanks,species),'volume_residual_l':volume_residual,
                      'global_mass_residual_g':{s:str(mass[s]-initial_mass[s]) for s in species}})
    final=snapshot(tanks,species)
    return {'schema_version':1,'initial':initial,'steps':trace,'final':final,
            'summary':{'tanks':len(tanks),'species':len(species),'steps':len(steps),'transfers':sum(len(s['transfers']) for s in steps),
                       'total_volume_l':str(initial_volume),'total_mass_g':{s:str(initial_mass[s]) for s in species},'conserved':True}}
