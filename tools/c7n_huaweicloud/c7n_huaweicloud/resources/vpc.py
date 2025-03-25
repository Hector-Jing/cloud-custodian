# Copyright The Cloud Custodian Authors.
# SPDX-License-Identifier: Apache-2.0

import logging

from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkvpc.v2 import ListPortsRequest, UpdateFlowLogRequest, UpdateFlowLogReq, UpdateFlowLogReqBody
from huaweicloudsdkvpc.v2 import DeleteFlowLogRequest, CreateFlowLogRequest, CreateFlowLogReq, CreateFlowLogReqBody
from huaweicloudsdkvpc.v3 import DeleteSecurityGroupRequest, DeleteSecurityGroupRuleRequest, ListSecurityGroupRulesRequest
from huaweicloudsdkvpc.v3 import BatchCreateSecurityGroupRulesRequest, BatchCreateSecurityGroupRulesOption, BatchCreateSecurityGroupRulesRequestBody

from c7n.exceptions import PolicyValidationError
from c7n.filters import Filter, ValueFilter
from c7n.utils import type_schema
from c7n_huaweicloud.actions.base import HuaweiCloudBaseAction
from c7n_huaweicloud.provider import resources
from c7n_huaweicloud.query import QueryResourceManager, TypeInfo

log = logging.getLogger("custodian.huaweicloud.resources.vpc")


@resources.register('vpc')
class Vpc(QueryResourceManager):
    class resource_type(TypeInfo):
        service = 'vpc'
        enum_spec = ('list_vpcs', 'vpcs', 'offset')
        id = 'id'
        tag_resource_type = 'vpcs'
        client_version = 'v2'


@resources.register('vpc-port')
class Port(QueryResourceManager):
    class resource_type(TypeInfo):
        service = 'vpc'
        enum_spec = ('list_ports', 'ports', 'offset')
        id = 'id'
        client_version = 'v2'
        tag_resource_type = ''


@resources.register('vpc-security-group')
class SecurityGroup(QueryResourceManager):
    class resource_type(TypeInfo):
        service = 'vpc'
        enum_spec = ('list_security_groups', 'security_groups', 'offset')
        id = 'id'
        tag_resource_type = 'security-groups'


@SecurityGroup.action_registry.register("delete")
class SecurityGroupDelete(HuaweiCloudBaseAction):
    """Action to delete vpc security groups.

    :example:

    .. code-block:: yaml

        policies:
          - name: security-group-delete-test-name
            resource: huaweicloud.vpc-security-group
            flters:
              - type: value
                key: name
                value: "sg-test"
            actions:
              - delete
    """

    schema = type_schema("delete")

    def perform_action(self, resource):
        client = self.manager.get_client()
        request = DeleteSecurityGroupRequest(security_group_id=resource["id"])
        response = client.delete_security_group(request)
        log.info("Delete security group %s response is: [%d] %s" % 
                 (resource["id"], response.status_code, response.to_json_object()))
        return response


@SecurityGroup.filter_registry.register("unattached")
class SecurityGroupUnAttached(Filter):
    """Filter to just vpc security groups that are not attached to any ports
    or are not default one.
    
    :example:

    .. code-block:: yaml

            policies:
              - name: security-groups-unattached
                resource: huaweicloud.vpc-security-group
                filters:
                  - unattached

    """

    schema = type_schema('unattached')

    def process(self, resources, event=None):
        sg_ids = [r['id'] for r in resources]
        sg_ids = list(set(sg_ids))
        client = self.manager.get_client('v2')
        try:
            request = ListPortsRequest(security_groups=sg_ids)
            response = client.list_ports(request)
        except exceptions.ClientRequestException as ex:
            log.exception(f"Unable to filter unattached security groups because query ports failed."
                          "RequestId: {ex.request_id}, Reason: {ex.error_msg}.")
        ports_object = response.ports
        ports = [p.to_dict() for p in ports_object]
        port_sgs = []
        for port in ports:
            port_sgs.extend(port['security_groups'])
        port_sgs = list(set(port_sgs))
        unattached = [r for r in resources if r['id'] not in port_sgs and r['name'] != 'default']

        return unattached


@resources.register('vpc-security-group-rule')
class SecurityGroupRule(QueryResourceManager):
    class resource_type(TypeInfo):
        service = 'vpc'
        enum_spec = ('list_security_group_rules', 'security_group_rules', 'offset')
        id = 'id'
        tag_resource_type = ''


class SecurityGroupRuleFilter(Filter):
    """Filter for verifying security group ingress and egress rules

    All attributes of a security group rule are available as
    value filters.

    If multiple attributes are specified the rule must satisfy
    all of them. Note that within an attribute match against a list value
    of a rule we default to or.

    If a group has any rules that match all conditions, then it
    matches the filter.

    Rules that match on the group are annotated onto the group and
    can subsequently be used by the remove-rules action.

    We have specialized handling for matching `InPorts` in ingress/egress
    rule `multiport`. The following example matches on ingress
    rules which allow for a range that includes all of the given ports.

    .. code-block:: yaml

      - type: ingress
        InPorts: [22, 443, 80]

    As well for verifying that a rule not allow for a specific set of ports
    as in the following example. The delta between this and the previous
    example is that if the rule allows for any ports not specified here,
    then the rule will match. ie. NotInPorts is a negative assertion match,
    it matches when a rule includes ports outside of the specified set.

    .. code-block:: yaml

      - type: ingress
        NotInPorts: [22]

    For simplifying ipranges handling which is specified as a list on a rule
    we provide a `Cidr` key which can be used as a value type filter evaluated
    against each of the rules. If any iprange cidr match then the permission
    matches.

    .. code-block:: yaml

      - type: ingress
        IpProtocol: -1
        FromPort: 445

    We also have specialized handling for matching self-references in
    ingress/egress permissions. The following example matches on ingress
    rules which allow traffic its own same security group.

    .. code-block:: yaml

      - type: ingress
        SelfReference: True

    `SGReferenceIds` can be used to filter out security group references in rules
    by a list of security group ids.

    .. code-block:: yaml

      - type: ingress
        SGReferenceIds: ['8fcdbf49-21b5-41a2-ad0e-51402828c443']

    We can also filter address group references based on the ids of refered
    address groups by `AGReferenceIds`.

    .. code-block:: yaml

      - type: ingress
        AGReferenceIds: ['fe2850f1-9bfe-41e6-be6d-3641a387ca27']

    By default, this filter matches a security group rule if
    _all_ of its keys match. Using `or`block causes a match
    if _any_ key matches. This can help consolidate some simple
    cases that would otherwise require multiple filters. To find
    security groups that allow all inbound traffic over IPv4 or IPv6,
    for example, we can use two filters inside an `or` block:

    .. code-block:: yaml

      - or:
        - type: ingress
          RemoteIpPrefix: "0.0.0.0/0"
        - type: ingress
          RemoteIpPrefix: "::/0"

    Note that evaluating _combinations_ of factors (e.g. traffic over
    port 22 from 0.0.0.0/0) still requires separate filters.
    """

    perm_attrs = {
        'RemoteIpPrefix', 'SGRuleIds', 'SecurityGroupIds', 'Descriptions', 
        'Ethertypes', 'Action', 'Priorities', 'Protocols', 'SGReferenceIds',
        'AGReferenceIds'}
    filter_attrs = {
        'InPorts', 'NotInPorts', 'SelfReference'}
    attrs = perm_attrs.union(filter_attrs)
    attrs.add('match-operator')

    def validate(self):
        delta = set(self.data.keys()).difference(self.attrs)
        delta.remove('type')
        if delta:
            raise PolicyValidationError("Unknown keys %s on %s" % (
                ", ".join(delta), self.manager.data))
        return self

    def process(self, resources, event=None):
        self.vfilters = []
        fattrs = list(sorted(self.perm_attrs.intersection(self.data.keys())))
        for f in fattrs:
            fv = self.data.get(f)
            if isinstance(fv, dict):
                fv['key'] = f
            else:
                fv = {f: fv}
            vf = ValueFilter(fv, self.manager)
            vf.annotate = False
            self.vfilters.append(vf)
        return super(SecurityGroupRuleFilter, self).process(resources, event)

    def process_direction(self, rule):
        return self.direction == rule['direction']

    def process_ips(self, rule):
        found = None
        if 'RemoteIpPrefix' in self.data:
            match_value = self.data['RemoteIpPrefix']
            found = ('remote_ip_prefix' in rule and match_value == rule['remote_ip_prefix']) or \
                    ('remote_ip_prefix' not in rule and str(match_value) == '-1')
        return found

    def process_protocols(self, rule):
        found = None
        if 'Protocols' in self.data:
            match_value = self.data['Protocols']
            if -1 in match_value:
                match_value.remove(-1)
                match_value.append('-1')
            protocol = rule['protocol'] if 'protocol' in rule else '-1'
            found = protocol in match_value
        return found

    def process_items(self, rule, filter_key, rule_key):
        found = None
        if filter_key in self.data:
            items = self.data[filter_key]
            if isinstance(items, list):
                found = rule_key in rule and rule[rule_key] in items
            elif isinstance(items, str):
                found = rule_key in rule and rule[rule_key] == items
        return found

    def process_ports(self, rule):
        found = None
        in_ports = self.data['InPorts'] if 'InPorts' in self.data else []
        not_in_ports = self.data['NotInPorts'] if 'NotInPorts' in self.data else []
        if not in_ports and not not_in_ports:
            return True
        multiport = rule.get('multiport', '-1')
        if multiport == '-1':
            return in_ports and not not_in_ports
        port_list = multiport.split(',')
        single_ports = []
        range_ports = []
        for port_item in port_list:
            if '-' in port_item:
                range_ports.append(port_item)
            else:
                single_ports.append(int(port_item))

        for port in in_ports:
            if port in single_ports:
                found = True
                continue
            else:
                found = any(port >= int(port_range.split('-')[0]) \
                            and port <= int(port_range.split('-')[1]) \
                            for port_range in range_ports)
            if found is False:
                break
        
        not_in_found = True
        for port in not_in_ports:
            if port in single_ports:
                not_in_found = False
                break
            else:
                not_in_found = all(port < int(port_range.split('-')[0]) \
                                   or port > int(port_range.split('-')[1]) \
                                   for port_range in range_ports)
        return found and not_in_found

    def process_self_reference(self, rule):
        found = None
        ref_match = self.data.get('SelfReference')
        if ref_match is not None:
            found = False
        if ref_match is True and 'remote_group_id' in rule:
            found = (rule['remote_group_id'] == rule['security_group_id'])
        if ref_match is False:
            found = ('remote_group_id' not in rule) or ('remote_group_id' in rule \
                    and rule['remote_group_id'] != rule['security_group_id'])
        return found

    def process_action(self, rule):
        found = None
        if filter_key in self.data:
            value = self.data[filter_key]
            found = rule_key in rule and rule[rule_key] in items
        return found

    def __call__(self, resource):
        matched = []
        match_op = self.data.get('match-operator', 'and') == 'and' and all or any
        perm_matches = {}
        
        perm_matches['direction'] = self.process_direction(resource)
        perm_matches['ips'] = self.process_ips(resource)
        perm_matches['sg_rule_ids'] = self.process_items(resource, 'SGRuleIds', 'id')
        perm_matches['sg_ids'] = self.process_items(resource, 'SecurityGroupIds', 
                                                    'security_group_id')
        perm_matches['descriptions'] = self.process_items(resource, 'Descriptions', 'description')
        perm_matches['ethertypes'] = self.process_items(resource, 'Ethertypes', 'ethertype')
        perm_matches['priorities'] = self.process_items(resource, 'Priorities', 'priority')
        perm_matches['sg_reference_ids'] = self.process_items(resource, 'SGReferenceIds', 
                                                              'remote_group_id')
        perm_matches['ag_reference_ids'] = self.process_items(resource, 'AGReferenceIds', 
                                                              'remote_address_group_id')
        perm_matches['protocols'] = self.process_protocols(resource)
        perm_matches['ports'] = self.process_ports(resource)
        perm_matches['self_reference'] = self.process_self_reference(resource)
        perm_matches['action'] = self.process_items(resource, 'Action', 'action')

        perm_match_values = list(filter(
            lambda x: x is not None, perm_matches.values()))
        # account for one python behavior any([]) == False, all([]) == True
        if match_op == all and not perm_match_values:
            return False

        match = match_op(perm_match_values)
        if match:
            matched.append(resource)

        if matched:
            matched_annotation = resource.setdefault('Matched%s' % self.direction.capitalize(), [])
            # If the same rule matches multiple filters, only add it to the match annotation
            # once. Note: Because we're looking for unique dicts and those aren't hashable,
            # we can't conveniently use set() to de-duplicate rules.
            
            return True


SGRuleSchema = {
    'match-operator': {'type': 'string', 'enum': ['or', 'and']},
    'RemoteIpPrefix': {
        'oneOf': [
            {'enum': [-1, '-1']},
            {'type': 'string'}
        ]
    },
    'SGRuleIds': {'type': 'array', 'items': {'type': 'string'}},
    'Descriptions': {'type': 'array', 'items': {'type': 'string'}},
    'SecurityGroupIds': {'type': 'array', 'items': {'type': 'string'}},
    'SGReferenceIds': {'type': 'array', 'items': {'type': 'string'}},
    'AGReferenceIds': {'type': 'array', 'items': {'type': 'string'}},
    'Ethertypes': {'type': 'array', 'items': {'type': 'string', 
                                              'enum': ['IPv4', 'IPv6', 'ipv4', 'ipv6']}},
    'Action': {'type': 'string', 'enum': ['allow', 'deny']},
    'Priorities': {'type': 'array', 'items': {'type': 'integer'}},
    'Protocols': {
        'type': 'array', 'items': {
            'oneOf': [
                {'enum': ['-1', 'tcp', 'udp', 'icmp', 'icmpv6']},
                {'type': 'integer'}
            ]
        }
    },
    'InPorts': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0, 'maximum': 65535}},
    'NotInPorts': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0, 'maximum': 65535}},
    'SelfReference': {'type': 'boolean'}
}


@SecurityGroupRule.filter_registry.register("ingress")
class SecurityGroupRuleIngress(SecurityGroupRuleFilter):
    direction = "ingress"
    schema = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {'type': {'enum': ['ingress']}},
        'required': ['type']}
    schema['properties'].update(SGRuleSchema)


@SecurityGroupRule.filter_registry.register("egress")
class SecurityGroupRuleEgress(SecurityGroupRuleFilter):
    direction = "egress"
    schema = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {'type': {'enum': ['egress']}},
        'required': ['type']}
    schema['properties'].update(SGRuleSchema)


@SecurityGroupRule.action_registry.register("delete")
class SecurityGroupRuleDelete(HuaweiCloudBaseAction):
    """Action to delete vpc security group rules.

    :example:

    .. code-block:: yaml

        policies:
          - name: security-group-rule-delete-tcp-22
            resource: huaweicloud.vpc-security-group-rule
            flters:
              - type: ingress
                RemoteIpPrefix: '0.0.0.0/0'
                Protocols: ['tcp']
                InPorts: [22]
            actions:
              - delete
    """

    schema = type_schema("delete")

    def perform_action(self, resource):
        client = self.manager.get_client()
        request = DeleteSecurityGroupRuleRequest(security_group_rule_id=resource["id"])
        response = client.delete_security_group_rule(request)
        log.info("Delete security group rule %s response is: [%d] %s" %
                 (resource["id"], response.status_code, response.to_json_object()))
        return response


@SecurityGroupRule.action_registry.register('remove-rules')
class RemoveSecurityGroupRules(HuaweiCloudBaseAction):
    """Action to remove ingress/egress rule(s) from a security group.

    :example:

    .. code-block:: yaml

            policies:
              - name: security-group-remove-tcp-8080-rules
                resource: huaweicloud.vpc-security-group-rule
                filters:
                  - type: ingress
                    Protocols: ['tcp']
                    InPorts: [8080]
                actions:
                  - type: remove-rules
                    ingress: matched

    """
    schema = type_schema(
        'remove-rules',
        ingress={'type': 'string', 'enum': ['matched', 'all']},
        egress={'type': 'string', 'enum': ['matched', 'all']})

    def process(self, resources):
        i_mode = self.data.get('ingress', 'matched')
        e_mode = self.data.get('egress', 'matched')

        client = self.manager.get_client()
        sg_ids = [r['security_group_id'] for r in resources]
        sg_ids = list(set(sg_ids))
        ret_rules = []
        for direction, mode in [('ingress', i_mode), ('egress', e_mode)]:
            rules = [r for r in resources if direction == r['direction']]
            # remove matched rules
            if mode == 'matched':
               self.perform_action(rules)
               ret_rules.extend(rules)
            # remove all rules in the security group of the matched rules
            elif mode == 'all':
                try:
                    request = ListSecurityGroupRulesRequest(security_group_id=sg_ids, 
                                                            direction=direction)
                    response = client.list_security_group_rules(request)
                except exceptions.ClientRequestException as ex:
                    log.exception(f"Unable to remove all rules because query {direction} rules "
                                  "failed. RequestId: {ex.request_id}, Reason: {ex.error_msg}.")
                    continue
                
                all_rules_object = response.security_group_rules
                all_rules = [r.to_dict() for r in all_rules_object]
                self.perform_action(all_rules)
                ret_rules.extend(all_rules)
            # remove rules with a list of rule filter conditions
            elif isinstance(mode, list):
                for f in mode:
                    try:
                        request = ListSecurityGroupRulesRequest(security_group_id=sg_ids, 
                                                                direction=direction)
                        f = dict(f)
                        for key, value in f.items():
                            setattr(request, key, value)
                        response = client.list_security_group_rules(request)
                        to_delete_rules_object = response.security_group_rules
                        to_delete_rules = [r.to_dict() for r in to_delete_rules_object]
                    except exceptions.ClientRequestException as ex:
                        log.exception(f"Unable to remove specified rules because query "
                                      "{direction} rules failed. "
                                      "RequestId: {ex.request_id}, Reason: {ex.error_msg}.")
                        continue
                    self.perform_action(to_delete_rules)
                    ret_rules.extend(to_delete_rules)

        return self.process_remove_result(ret_rules)

    def process_remove_result(self, resources):
        remove_result = {"remove_succeeded_rules": [], "remove_failed_rules": self.failed_resources}
        remove_result.get("remove_succeeded_rules").extend(resources)
        print(remove_result)
        return remove_result

    def perform_action(self, rules):
        client = self.manager.get_client()
        for r in rules:
            try:
                request = DeleteSecurityGroupRuleRequest(security_group_rule_id=r["id"])
                response = client.delete_security_group_rule(request)
            except exceptions.ClientRequestException as ex:
                res = r.get("id")
                log.exception(f"Unable to submit action against the resource - {res} "
                              "RequestId: {ex.request_id}, Reason: {ex.error_msg}")
                self.handle_exception(r, rules)


@SecurityGroupRule.action_registry.register('set-rules')
class SetSecurityGroupRules(HuaweiCloudBaseAction):
    """Action to add/remove ingress/egress rule(s) to a security group

    :example:

    .. code-block:: yaml

       policies:
         - name: security-group-set-rules
           resource: huaweicloud.vpc-security-group-rule
           filters:
            - type: ingress
              RemoteIpPrefix: '192.168.21.0/24'
              Protocols: ['tcp']
              InPorts: [8080]
           actions:
            - type: set-rules
              # remove the rule matched by a previous ingress filter.
              remove-ingress: matched
              # remove rules by specifying them fully, ie remove default outbound
              # access.
              remove-egress:
                - action: allow
                  remote_ip_prefix: '0.0.0.0/0'
                - action: allow
                  remote_ip_prefix: '::/0'

              # add a list of rules to the security group.
              add-ingress:
                # full syntax/parameters to create rules can be used.
                - ethertype: ipv4
                  multiport: '22'
                  remote_ip_prefix: '192.168.22.0/24'
                  protocol: tcp
                - ethertype: ipv4
                  protocol: tcp
                  multiport: '3389'
                  remote_ip_prefix: '10.0.0.0/8'
                  action: allow
                  priotity: 1
              # add a list of egress rules to a security group
              add-egress:
                - ethertype: ipv4
                  multiport: '22'
                  remote_ip_prefix: '192.168.22.0/24'
                  protocol: tcp
    """
    schema = type_schema(
        'set-rules',
        **{'add-ingress': {'type': 'array', 'items': {'type': 'object', 'minProperties': 1}},
           'remove-ingress': {'oneOf': [
               {'enum': ['all', 'matched']},
               {'type': 'array', 'items': {'type': 'object', 'minProperties': 2}}]},
           'add-egress': {'type': 'array', 'items': {'type': 'object', 'minProperties': 1}},
           'remove-egress': {'oneOf': [
               {'enum': ['all', 'matched']},
               {'type': 'array', 'items': {'type': 'object', 'minProperties': 2}}]}}
    )

    def process(self, resources):
        i_rules = self.data.get('add-ingress', ())
        e_rules = self.data.get('add-egress', ())

        sg_ids = [r['security_group_id'] for r in resources]
        sg_ids = list(set(sg_ids))
        client = self.manager.get_client()
        ret_rules = []
        # add rules
        for sg_id in sg_ids:
            try:
                request = BatchCreateSecurityGroupRulesRequest()
                request.security_group_id = sg_id
                create_rules = []
                for direction, rules in [('ingress', i_rules), ('egress', e_rules)]:
                    for r in rules:
                        rule_option = BatchCreateSecurityGroupRulesOption(direction=direction)
                        r = dict(r)
                        for key, value in r.items():
                            setattr(rule_option, key, value)
                        create_rules.append(rule_option)
                if not create_rules:
                    continue
                request.body = \
                    BatchCreateSecurityGroupRulesRequestBody(security_group_rules=create_rules)
                response = client.batch_create_security_group_rules(request)
            except exceptions.ClientRequestException as ex:
                log.exception(f"Unable to add rules in security group {sg_id}. "
                              "RequestId: {ex.request_id}, Reason: {ex.error_msg}")
                continue
            res_rules_object = response.security_group_rules
            res_rules = [r.to_dict() for r in res_rules_object]
            ret_rules.extend(res_rules)

        # remove rules
        remover = RemoveSecurityGroupRules(
            {'ingress': self.data.get('remove-ingress', ()),
             'egress': self.data.get('remove-egress', ())}, self.manager)
        remove_result = remover.process(resources)

        return self.process_multi_result(ret_rules, remove_result)

    def process_multi_result(self, add_rules, remove_result):
        multi_result = {"add_succeeded_rules": [], "add_failed_rules": []}
        multi_result.get("add_succeeded_rules").extend(add_rules)
        multi_result.update(remove_result)
        print(multi_result)
        return multi_result

    def perform_action(self, resource):
        return None


@resources.register('vpc-flow-log')
class FlowLog(QueryResourceManager):
    class resource_type(TypeInfo):
        service = 'vpc'
        enum_spec = ('list_flow_logs', 'flow_logs', 'offset')
        id = 'id'
        client_version = 'v2'
        tag_resource_type = ''


@FlowLog.action_registry.register("set-flow-log")
class SetFlowLog(HuaweiCloudBaseAction):
    """Action to set flow logs for a network resource.

    :example:

    .. code-block:: yaml

        policies:
          - name: vpc-enable-flow-logs
            resource: huaweicloud.vpc-flow-log
            flters:
              - type: value
                key: resource_type
                value: vpc
              - type: value
                key: resource_type
                value: DOWN
            actions:
              - type: set-flow-log
                action: enable
    
    `create-attrs` can be used to create flow logs of the specified 
    network resources. You don't need to input `resource_type` and 
    `resource_id` params, for example create a flow log to collect 
    all traffic:

    .. code-block:: yaml

      - type: set-flow-log
        action: create
        create-attrs:
          - traffic_type: all
            log_group_id: '324d2393-7d89-4262-88b1-c5d3497d5f54'
            log_topic_id: '2fa117ad-3452-4367-b360-88cb89f8a561'

    """
    

    schema = type_schema(
        'set-flow-log',
        required=['action'],
        **{'action': {'enum': ['enable', 'disable', 'create', 'delete']},
           'create-attrs': {'type': 'array', 'items': {'type': 'object'}}})

    def process(self, resources):
        action = self.data['action']
        client = self.manager.get_client('v2')
        ret_fls = []
        if action in ['enable', 'disable']:
            admin_state = True if action == 'enable' else False
            for fl in resources:
                try:
                    request = UpdateFlowLogRequest(flowlog_id=fl['id'])
                    fl_body = UpdateFlowLogReq(admin_state=admin_state)
                    request.body = UpdateFlowLogReqBody(flow_log=fl_body)
                    response = client.update_flow_log(request)
                    resp_fl = response.flow_log
                    ret_fls.append(resp_fl.to_dict())
                except exceptions.ClientRequestException as ex:
                    log.exception(f"Failed to {action} flow log. "
                                  "RequestId: {ex.request_id}, Reason: {ex.error_msg}.")
                    self.handle_exception(fl, resources)
        elif action == 'delete':
            for fl in resources:
                try:
                    request = DeleteFlowLogRequest(flowlog_id=fl['id'])
                    response = client.delete_flow_log(request)
                    ret_fls.append(fl)
                except exceptions.ClientRequestException as ex:
                    log.exception(f"Failed to {action} flow log. "
                                  "RequestId: {ex.request_id}, Reason: {ex.error_msg}.")
                    self.handle_exception(fl, resources)
        elif action == 'create':
            req_fls = self.data.get('create-attrs', ())
            resource_ids = [f['resource_id'] for f in resources]
            resource_ids = list(set(resource_ids))
            if not resource_ids:
                return self.process_fl_result(ret_fls, action)
            resource_type = resources[0]['resource_type']
            for r in resource_ids:
                for fl in req_fls:
                    try:
                        request = CreateFlowLogRequest()
                        fl_body = CreateFlowLogReq(resource_type=resource_type, resource_id=r)
                        fl = dict(fl)
                        for key, value in fl.items():
                            setattr(fl_body, key, value)
                        request.body = CreateFlowLogReqBody(flow_log=fl_body)
                        response = client.create_flow_log(request)
                        resp_fl = response.flow_log
                        ret_fls.append(resp_fl.to_dict())
                    except exceptions.ClientRequestException as ex:
                        log.exception(f"Failed to {action} flow log. "
                                      "RequestId: {ex.request_id}, Reason: {ex.error_msg}.")

        return self.process_fl_result(ret_fls, action)

    def perform_action(self, resource):
        return None

    def process_fl_result(self, resources, action):
        action_result = {"action": action}
        self.result.get("succeeded_resources").extend(resources)
        self.result.update(action_result)
        print(self.result)
        return self.result
