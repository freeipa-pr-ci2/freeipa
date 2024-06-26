It is now possible to allow constrained delegation of credentials so
that a service can impersonate a user when communicating with another
service w/o requiring the user to actually forward their TGT.
This makes for a much better method of delegating credentials as it
prevents exposure of the short term secret of the user.

I added a relatively simple access control method that allow the KDC to
decide exactly which services are allowed to impersonate which users
against other services. A simple grouping mechanism is used so that in
large environments, clusters and otherwise classes of services can be
much more easily managed.

The grouping mechanism has been built so that lookup is highly optimized
and is basically reduced to a single search that uses the derefernce
control.

The grouping mechanism is very simple a groupOfPrincipals object is
introduced, this Auxiliary class have a single optional attribute called
memberPrincipal which is a string containing a principal name.

A separate objectclass is also introduced called ipaKrb5DelegationACL,
it is a subclass of groupOfPrincipals and is a Structural class.

It has 2 additional optional attributes: ipaAllowedTarget and
ipaAllowToImpersonate. They are both DNs.

The memberPrincipal attribute in this class contains the list of
principals that are being considered proxies[1]. That is: the
principals of the services that want to impersonate client principals
against other services.

The ipaAllowToImpersonate must point to a groupOfPrincipal based
object that contains the list of client principals (normally these are
user principals) that can be impersonated by this service.
If the attribute is missing than the service is allowed to impersonate
*any* user.

The ipaAllowedTarget DN must point to a groupOfPrincipal based object
that contains the list of service principals that the proxy service is
allowed target when impersonating users. A target must be specified in
order to allow a service to access it impersonating another principal.


At the moment no wildcarding is implemented so services have to be
explicitly listed in their respective groups.
I have some idea of adding wildcard support at least for the
ipaAllowToImpersonate group in order to separate user principals by
REALM. So you can say all users of REALM1 can be impersonated by this
service but no users of REALM2.

It is unclear how this wildcarding may be implemented, but it must be
simple to avoid potentially very expensive computations every time a
ticket for the target services is requested.

I have briefly tested this patch by manually creating a few objects then
using the kvno command to test that I could get a ldap ticket just using
the HTTP credentials (in order to do this I had to allow also s4u2self
operations for the HTTP service, but this is *not* generally required
and it is *not* desired in the IPA framework implementation).

This patchset does not contain any CLI or UI nor installation changes to
create ipaKrb5DelegationACL obujects. It is indeed yet unclear where we
want to store them (suggestions are welcome) and how/when we may want to
expose this mechanism through UI/CLI for general usage.

The initial intended usage is to allow us to move away from using
forwarded TGTs in the IPA framework and instead use S4U2Proxy in order
to access the ldap service. In order to do this some changes will need
to be made in installation scripts and replica management scripts later.

How to test:

Create 2 objects like these:

dn: cn=ipa-http-delegation,...
objectClass: ipaKrb5DelegationACL
objectClass: groupOfPrincipals
cn: ipa-http-delegation
memberPrincipal: HTTP/ipaserver.example.com@EXAMPLE.COM
ipaAllowedTarget: cn=ipa-ldap-delegation-targets,...

dn: cn=ipa-ldap-delegation-targets,...
objectClass: groupOfPrincipals
cn: ipa-ldap-delegation-targets
memberPrincipal: ldap/ipaserver.example.com@EXAMPLE.COM


In order to test with kvno which pretend to do s4u2self too you will
need to allow the HTTP service to impersonate arbitrary users.

This is done with:
kdamin.local
modprinc +ok_to_auth_as_delegate HTTP/ipaserver.example.com

NOTE: Do not grant +ok_to_auth_as_delegate in production without
carefully considering the outcome. This flags grants a service the
ability to impersonate any user to itself, which, combined with the
permission to proxy, means it will be allowed to impersonate any user
to the target service w/o any explicit user permission/delegation.
This flag is *NOT* necessary to permit proxying, it is used in this
example only because the kvno utility is hardwired to test both s4u2self
and s4u2proxy at the same time and would fail to operate without it.

Then run kvno as follows:

# Init credntials as HTTP
kinit -kt /etc/httpd/conf/ipa.keytab HTTP/ipaserver.example.com

# Perform S4U2Self
kvno -U admin HTTP/ipaserver.example.com

# Perform S4U2Proxy
kvno -U admin -P ldap/ipaserver.example.com


If this works it means you successfully impersonated the admin user with
the HTTP service against the ldap service.

Cleanup by removing the self-impersonation flag:
modprinc -ok_to_auth_as_delegate HTTP/ipaserver.example.com

Simo.


If IPA is compiled with krb5 1.20 and newer (KDB DAL >= 9), then the
behavior of S4U2Self changes: S4U2Self TGS-REQs produce forwardable
tickets for all requesters, except if the requester principal is set as
the proxy (impersonating service) in at least one `servicedelegation`
rule. In this case, even if the rule has no target, the KDC will
response to S4U2Self requests with a non-forwardable ticket. Hence,
granting the `ok_to_auth_as_delegate` permission to the proxy service
remains the only way for this service to obtain the evidence ticket
required for general constrained delegation requests if this ticket is
not provided by the client.


[1]
Note that here I use the term proxy in a different way than it is used in
the krb interfaces. It may seem a bit confusing but I think people will
understand it better this way.

In this document 'client' connects to 'proxy' which impersonates 'client'
against 'service'.
In the Code/API the 'client' connects to 'server' which impersonates
'client' against 'proxy'.
