"""Read the declared XD contract and ROS master registrations without commanding a vehicle."""
import rosgraph
import rospy


def describe_interfaces(contract):
    publishers, subscribers, services = rosgraph.Master(rospy.get_name()).getSystemState()
    registrations = {'publisher': dict(publishers), 'subscriber': dict(subscribers), 'service': dict(services)}
    vehicles = {}
    for vehicle, namespace in contract['vehicles'].items():
        interfaces = {}
        for key, endpoint in contract['endpoints'].items():
            path = namespace + '/' + endpoint['name']
            nodes = registrations[endpoint['requires']].get(path, [])
            interfaces[key] = dict(path=path, expected_type=endpoint['type'], registered=bool(nodes), nodes=nodes)
        vehicles[vehicle] = interfaces
    return dict(vehicles=vehicles, tools=[d['function']['name'] for d in contract['tools']],
                frame=contract['frame'], availability='ROS master registration; flight readiness uses xd_state')
