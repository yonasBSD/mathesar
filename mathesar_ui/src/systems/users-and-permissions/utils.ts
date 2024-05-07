import { iconAdminUser, iconUser } from '@mathesar/icons';
import type { UserModel } from '@mathesar/stores/users';
import { MissingExhaustiveConditionError } from '@mathesar/utils/errors';
import type { IconProps } from '@mathesar-component-library/types';

export type UserType = 'admin' | 'standard';

export function getDisplayNameForUserType(userType: UserType): string {
  switch (userType) {
    case 'admin':
      return 'Admin';
    case 'standard':
      return 'Standard';
    default:
      throw new MissingExhaustiveConditionError(userType);
  }
}

export function getUserTypeInfoFromUserModel(userModel: UserModel): {
  icon: IconProps;
  type: UserType;
  displayName: string;
} {
  const type: UserType = userModel.isSuperUser ? 'admin' : 'standard';
  const displayName = getDisplayNameForUserType(type);
  const icon = userModel.isSuperUser ? iconAdminUser : iconUser;
  return {
    icon,
    type,
    displayName,
  };
}
